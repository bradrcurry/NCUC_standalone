"""Document intelligence sub-app: Docling, classification, embedding,
ollama LLM probe, gold-set / baseline training, and overnight doc-intel
runs.

Wired into the main CLI as `duke-rates doc-intel <command>`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import typer

from duke_rates.config import get_settings
from duke_rates.db.repository import Repository
from duke_rates.db.sqlite import connect as connect_sqlite
from duke_rates.models.document import DocumentCategory, DocumentKind

from duke_rates.cli_commands._cli_utils import (
    _bootstrap,
    _format_optional_pct,
    _safe_cli_text,
)


logger = logging.getLogger(__name__)

doc_intel_app = typer.Typer(help="Document intelligence: Docling, classification, embedding, LLM probe, gold-set training.")


# Helpers (only doc-intel uses these)

def _write_gold_set_jsonl(path: Path, gold_candidates: list[dict], conn, settings) -> int:
    """Write gold-set rows enriched with a text sample to ``path``. Returns count written.

    The text sample is loaded the same way the bulk extractor sees text:
    docling artifact (full or sliced) preferred, pdfplumber as fallback.
    Truncated to 2000 chars to keep the file size reasonable for training.
    """
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import (
        BulkExtractor, normalize_docling_markdown, normalize_ocr_text,
    )
    extractor = BulkExtractor(db_path=str(settings.database_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding="utf-8") as f:
        for cand in gold_candidates:
            doc = extractor.get_document_for_extraction(cand["hd_id"])
            if not doc:
                continue
            try:
                text, src = extractor.extract_text_from_pdf(
                    doc["local_path"],
                    start_page=doc.get("start_page"),
                    end_page=doc.get("end_page"),
                )
                if src in ("docling_artifact", "docling_artifact_sliced"):
                    text = normalize_docling_markdown(text)
                text = normalize_ocr_text(text)
            except Exception:
                text = ""
            row = {
                **cand,
                "text_sample": (text or "")[:2000],
                "text_source": src if text else "none",
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
    return written


# Commands

@doc_intel_app.command("show-document-classification-audit")
def show_document_classification_audit_nc(
    limit: int = typer.Option(25, "--limit", help="Max classified rows to show."),
    company: str | None = typer.Option(None, "--company", help="Optional company filter."),
    family_key: str | None = typer.Option(None, "--family-key", help="Optional family filter."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show NC document buckets for routing beyond simple tariff-charge extraction."""
    from duke_rates.historical.ncuc.document_classification_audit import (
        build_document_classification_audit_report,
    )

    _, repository = _bootstrap()
    report = build_document_classification_audit_report(
        repository,
        limit=limit,
        company=company,
        family_key=family_key,
    )

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo("Document Classification Audit (NC)")
    typer.echo(f"  historical_documents={report['summary']['historical_document_count']}")
    typer.echo("  bucket_counts:")
    for row in report["summary"]["bucket_counts"]:
        typer.echo(f"    {row['document_bucket']}={row['count']}")
    top_profiles = ", ".join(
        f"{row['parser_profile']}:{row['count']}"
        for row in report["summary"]["top_parser_profiles"][:5]
    ) or "-"
    typer.echo(f"  top_parser_profiles={top_profiles}")

    typer.echo("\nClassified Rows")
    if not report["rows"]:
        typer.echo("  none")
        return
    for row in report["rows"]:
        typer.echo(
            "  "
            f"id={row['historical_document_id']} "
            f"bucket={row['document_bucket']} "
            f"family={row['family_key'] or '-'} "
            f"profile={row['parser_profile']} "
            f"charges={row['charge_count']}"
        )
        typer.echo(
            "    "
            f"status={row['processing_status'] or '-'} "
            f"outcome={row['outcome_quality'] or '-'} "
            f"reason={row['classification_reason']}"
        )
        if row.get("document_bucket") == "needs_normalization":
            typer.echo(
                "    "
                f"raw_text_chars={row['raw_text_chars']} "
                f"pages={row['page_count']} "
                f"lane={row['normalization_lane']}"
            )
        if row.get("document_bucket") == "needs_processing":
            typer.echo(
                "    "
                f"raw_text_chars={row['raw_text_chars']} "
                f"pages={row['page_count']}"
            )
        if row.get("filing_classification"):
            typer.echo(f"    filing_classification={row['filing_classification']}")
        if row.get("skip_reason"):
            typer.echo(f"    skip_reason={row['skip_reason']}")
        if row.get("is_redline_candidate"):
            typer.echo(f"    redline_confidence={row['redline_confidence']}")
        typer.echo(f"    title={(row['title'] or '')[:100]}")


@doc_intel_app.command("show-unknown-routing-audit")
def show_unknown_routing_audit_nc(
    limit: int = typer.Option(25, "--limit", help="Max family-level routing rows to show."),
    company: str | None = typer.Option(None, "--company", help="Optional company filter."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Rank NC families still landing in `unknown` or weak fallback routing buckets."""
    from duke_rates.historical.ncuc.document_classification_audit import (
        build_unknown_routing_audit_report,
    )

    _, repository = _bootstrap()
    report = build_unknown_routing_audit_report(
        repository,
        limit=limit,
        company=company,
    )

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo("Unknown Routing Audit (NC)")
    typer.echo(
        "  "
        f"problem_documents={report['summary']['problem_document_count']} "
        f"problem_families={report['summary']['problem_family_count']}"
    )
    action_counts = ", ".join(
        f"{row['recommended_action']}={row['count']}"
        for row in report["summary"]["recommended_action_counts"]
    ) or "-"
    typer.echo(f"  recommended_action_counts={action_counts}")

    if not report["rows"]:
        typer.echo("  none")
        return

    typer.echo("\nFamily Routing Rows")
    for row in report["rows"]:
        typer.echo(
            "  "
            f"family={row['family_key']} docs={row['document_count']} "
            f"action={row['recommended_action']}"
        )
        typer.echo(
            "    "
            f"company={row['company'] or '-'} "
            f"profile={row['top_parser_profile']} "
            f"filing_class={row['top_filing_classification']}"
        )
        if row.get("recommended_action") == "enqueue_ocr_remediation":
            typer.echo(f"    normalization_lane={row.get('top_normalization_lane') or '-'}")
        typer.echo(f"    reason={row['reason']}")
        typer.echo(f"    title={(row['sample_title'] or '')[:100]}")
        if row.get("synthesized_profile_name"):
            typer.echo(
                "    "
                f"candidate_profile={row['synthesized_profile_name']} "
                f"kind={row.get('synthesized_profile_kind') or '-'}"
            )
            typer.echo(f"    synthesis_reason={row.get('synthesized_profile_reason') or '-'}")
            if row.get("synthesized_next_command"):
                typer.echo(f"    next={row['synthesized_next_command']}")


@doc_intel_app.command("mine-docling")
def mine_docling_nc(
    limit: int = typer.Option(50, help="Max Docling artifacts to process."),
    accelerator: str = typer.Option("cuda", help="Accelerator used for Docling conversion: cpu or cuda."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be processed without running."),
    record_id: int | None = typer.Option(None, help="Process a single discovery record only."),
    skip_extraction: bool = typer.Option(False, "--skip-extraction", help="Skip BulkExtractor after family matching (pipeline only)."),
) -> None:
    """Bridge stored Docling artifacts into the NCUC page-aware parsing pipeline.

    For each successfully-converted Docling artifact with stored JSON content:
    1. Reconstructs PageEvidence from stored Docling JSON
    2. Feeds pages through NcucPipelineImporter (family match + historical doc creation
       with all guardrails: provisional family filters, weak-match rejection, hint recovery)
    3. Runs BulkExtractor.process_document() on each created historical doc
       (creates document_fingerprints, parse_attempt_logs, parse_review_outcomes, tariff_charges)

    This is a selective operator path — not run by default on every import.
    """
    import sqlite3 as _sqlite3
    import time as _time
    from datetime import UTC, datetime
    from pathlib import Path as _Path

    from duke_rates.historical.ncuc.pipeline.docling_page_miner import (
        mine_pages_from_docling_artifact,
    )
    from duke_rates.historical.ncuc.pipeline.stage_versions import (
        DOCLING_PAGE_MINER_VERSION,
    )
    from duke_rates.historical.ncuc.importer import NcucPipelineImporter
    from duke_rates.models.ncuc import NcucDiscoveryRecord, NcucFetchStatus, NcucFilingClassification

    settings, repository = _bootstrap()
    from duke_rates.config import get_settings as _get_settings

    # Build query for Docling artifacts to process
    _db_path = settings.database_path
    _conn_probe = _sqlite3.connect(_db_path)
    _conn_probe.row_factory = _sqlite3.Row
    try:
        if record_id:
            rows = _conn_probe.execute(
                """
                SELECT d.id, d.local_path, d.content_hash, d.filing_title, d.filing_date,
                       d.docket_number, d.utility,
                       da.doc_json_content, da.plain_text_content,
                       da.tables_json_content, da.page_count, da.accelerator, da.pipeline,
                       da.file_hash
                FROM docling_artifacts da
                JOIN ncuc_discovery_records d ON da.source_pdf = d.local_path
                WHERE d.id = ?
                  AND da.status IN ('success', 'ConversionStatus.SUCCESS', 'ConversionStatus.PARTIAL_SUCCESS')
                  AND da.doc_json_content IS NOT NULL
                """,
                (record_id,),
            ).fetchall()
        else:
            rows = _conn_probe.execute(
                """
                SELECT d.id, d.local_path, d.content_hash, d.filing_title, d.filing_date,
                       d.docket_number, d.utility,
                       da.doc_json_content, da.plain_text_content,
                       da.tables_json_content, da.page_count, da.accelerator, da.pipeline,
                       da.file_hash
                FROM docling_artifacts da
                LEFT JOIN ncuc_discovery_records d ON da.source_pdf = d.local_path
                WHERE da.status IN ('success', 'ConversionStatus.SUCCESS', 'ConversionStatus.PARTIAL_SUCCESS')
                  AND da.doc_json_content IS NOT NULL
                  AND da.accelerator = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM ncuc_page_artifacts pa
                      WHERE pa.source_pdf = da.source_pdf
                        AND pa.artifact_version = ?
                  )
                LIMIT ?
                """,
                (accelerator, DOCLING_PAGE_MINER_VERSION, limit),
            ).fetchall()
        rows = list(rows)
    finally:
        _conn_probe.close()

    total = len(rows)
    if dry_run:
        typer.echo(f"Would process {total} Docling artifact(s):")
        for row in rows[:50]:
            typer.echo(f"  {row['local_path']}")
        if total > 50:
            typer.echo(f"  ... and {total - 50} more")
        return

    typer.echo(f"Processing {total} Docling artifact(s) with accelerator={accelerator}")
    typer.echo("Press Ctrl+C to stop — progress is committed after each record.\n")

    importer = NcucPipelineImporter(settings, repository)

    done = 0
    total_docs = 0
    total_charges = 0

    try:
        for i, row in enumerate(rows, 1):
            disc_id = row["id"]
            local_path = row["local_path"]
            content_hash = row["content_hash"]
            filing_title = row["filing_title"]
            filing_date = row["filing_date"]
            docket_number = row["docket_number"]
            utility = row["utility"] or "Duke Energy Progress"
            doc_json = row["doc_json_content"]
            plain_text = row["plain_text_content"]
            tables_json = row["tables_json_content"]
            page_count = row["page_count"]
            accel = row["accelerator"]
            pipeline = row["pipeline"]
            file_hash = row["file_hash"]

            if not local_path:
                typer.echo(f"  [{i}/{total}] SKIP (no local_path)")
                continue

            typer.echo(f"  [{i}/{total}] {local_path}", nl=False)
            t0 = _time.perf_counter()

            # Step 1: Reconstruct PageEvidence from stored Docling JSON
            artifact = {
                "doc_json_content": doc_json,
                "plain_text_content": plain_text,
                "tables_json_content": tables_json,
                "page_count": page_count,
                "accelerator": accel,
                "pipeline": pipeline,
                "file_hash": file_hash,
            }
            pages, page_metadata = mine_pages_from_docling_artifact(artifact)
            if not pages:
                typer.echo(" FAIL (no pages reconstructed)")
                continue

            # Step 2: Build a thin NcucDiscoveryRecord so the importer has context
            # for company inference, hint seeding, and provisional family creation.
            synth_record = NcucDiscoveryRecord(
                id=disc_id,
                local_path=local_path,
                content_hash=content_hash or file_hash,
                discovered_url=f"docling://{local_path}",
                filing_title=filing_title or _Path(local_path).stem,
                filing_date=filing_date or "1970-01-01",
                docket_number=docket_number,
                utility=utility,
                filing_classification=NcucFilingClassification.TARIFF_SHEETS,
                fetch_status=NcucFetchStatus.SUCCESS,
                fetched_at=datetime.now(UTC),
            )

            # Step 3: Feed into importer — runs Stage B save + Stage C segmentation
            # + full Stages D–G (all guardrails, hints, provisional families).
            created_ids = importer.mine_discovery_record_spans_with_pages(
                synth_record,
                pages,
                page_artifact_version=DOCLING_PAGE_MINER_VERSION,
                page_metadata=page_metadata,
            )

            docs_this = len(created_ids)
            charges_this = 0

            # Step 4: Run BulkExtractor on each created historical document
            if created_ids and not skip_extraction:
                from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
                extractor = BulkExtractor(db_path=_db_path)
                for doc_id in created_ids:
                    doc = extractor.get_document_for_extraction(doc_id)
                    if doc:
                        try:
                            _, _, num_inserted, *_ = extractor.process_document(doc)
                            charges_this += num_inserted
                        except Exception as exc:
                            typer.echo(f"\n    WARN extraction failed for doc {doc_id}: {exc}")

            elapsed = _time.perf_counter() - t0
            typer.echo(
                f" OK  pages={len(pages)} docs={docs_this} charges={charges_this} t={elapsed:.1f}s"
            )
            total_docs += docs_this
            total_charges += charges_this
            done += 1

    except KeyboardInterrupt:
        typer.echo("\nInterrupted.")

    typer.echo(
        f"\nDone: {done}/{total} processed, {total_docs} historical docs, {total_charges} charges."
    )


@doc_intel_app.command("run-docling")
def run_docling_nc(
    pdf_path: str = typer.Argument(..., help="Path to a local PDF file to convert with Docling."),
    accelerator: str = typer.Option("cpu", help="Accelerator: cpu, cuda, or mps."),
    force: bool = typer.Option(False, "--force", help="Re-run even if cached artifacts exist."),
    persist: bool = typer.Option(True, "--persist/--no-persist", help="Save artifact record to DB."),
    scanned: bool = typer.Option(False, "--scanned", help="Enable Tesseract OCR (document has scanned pages)."),
    full_ocr: bool = typer.Option(False, "--full-ocr", help="Force OCR on every page (fully scanned document)."),
) -> None:
    """Convert a single PDF with Docling (standard pipeline) and cache the structured artifacts.

    This is a selective pilot command. Do NOT use it on every document.
    Use only for OCR-heavy, table-heavy, or repeatedly weak-parse documents.

    Uses the standard pipeline: docling-layout-heron + TableFormer ACCURATE + Tesseract OCR.
    For hard pre-1990 scans, use doc-intel run-docling-vlm instead.

    When --persist is set (default), content is stored in the docling_artifacts DB table
    (doc_json_content, plain_text_content, tables_json_content columns) — no sidecar files
    are written to disk.  Use --no-persist to disable DB storage (sidecar fallback mode).
    """
    from duke_rates.historical.ncuc.pipeline.docling_backend import (
        PIPELINE_STANDARD,
        convert_pdf_safe,
        get_docling_unavailable_reason,
    )
    from duke_rates.db.sqlite import connect
    from duke_rates.hardware.cpu_config import configure_torch_inference, warmup_gpu

    unavailable = get_docling_unavailable_reason()
    if unavailable:
        typer.echo(f"Docling unavailable: {unavailable}")
        raise typer.Exit(code=1)

    configure_torch_inference()
    if accelerator == "cuda":
        warmup_gpu()

    settings, _ = _bootstrap()

    typer.echo(f"Running Docling on: {pdf_path}")
    typer.echo(f"  accelerator={accelerator}  scanned={scanned}  full_ocr={full_ocr}  force={force}")

    db_conn = None
    if persist:
        db_conn = connect(settings.database_path)

    try:
        result = convert_pdf_safe(
            pdf_path,
            accelerator=accelerator,
            force=force,
            has_scanned_pages=scanned or full_ocr,
            force_full_page_ocr=full_ocr,
            conn=db_conn,
        )
    finally:
        if db_conn is not None:
            db_conn.close()

    if result is None:
        typer.echo("Docling conversion failed. See logs for details.")
        raise typer.Exit(code=1)

    tables_count = len(result.get("tables") or [])
    storage = "db" if result.get("json_path") is None else "sidecar"
    typer.echo(f"Conversion status : {result['conversion_status']}")
    typer.echo(f"Pages             : {result['page_count']}")
    typer.echo(f"Pipeline          : {result.get('pipeline', 'standard')}")
    typer.echo(f"Tables            : {tables_count}")
    typer.echo(f"Storage           : {storage}")
    if result.get("json_path"):
        typer.echo(f"JSON sidecar      : {result['json_path']}")
        typer.echo(f"Text sidecar      : {result['plain_text_path']}")
        typer.echo(f"Tables sidecar    : {result['tables_path']}")


@doc_intel_app.command("run-docling-vlm")
def run_docling_vlm(
    pdf_path: str = typer.Argument(..., help="Path to a PDF file to convert with the VLM pipeline."),
    accelerator: str = typer.Option("cuda", help="Accelerator: cuda or cpu (cuda strongly recommended)."),
    force: bool = typer.Option(False, "--force", help="Re-run even if cached artifacts exist."),
    max_pages: int = typer.Option(0, help="Limit to first N pages (0 = all pages)."),
) -> None:
    """Convert a hard scanned PDF using the SmolDocling/GraniteDocling VLM pipeline.

    Use this for pre-1990 scanned filings where standard OCR (Tesseract) produces
    poor results. The VLM pipeline treats each page as a vision task, reading it
    end-to-end like a human rather than running separate layout detection + OCR.

    Requires CUDA for practical throughput (~30-120s/page on CPU vs ~5-15s/page on GPU).
    """
    from duke_rates.historical.ncuc.pipeline.docling_backend import (
        PIPELINE_VLM,
        convert_pdf_with_docling,
        get_docling_unavailable_reason,
    )
    from duke_rates.hardware.cpu_config import configure_torch_inference, warmup_gpu

    unavailable = get_docling_unavailable_reason()
    if unavailable:
        typer.echo(f"Docling unavailable: {unavailable}")
        raise typer.Exit(code=1)

    configure_torch_inference()
    if accelerator == "cuda":
        warmup_gpu()

    typer.echo(f"Running Docling VLM pipeline on: {pdf_path}")
    typer.echo(f"  accelerator={accelerator}  force={force}")

    result = convert_pdf_with_docling(
        pdf_path,
        accelerator=accelerator,
        pipeline=PIPELINE_VLM,
        force=force,
        max_pages=max_pages if max_pages > 0 else None,
    )
    if result is None:
        typer.echo("VLM conversion failed. See logs for details.")
        raise typer.Exit(code=1)

    typer.echo(f"Conversion status : {result['conversion_status']}")
    typer.echo(f"Pages             : {result['page_count']}")
    typer.echo(f"Pipeline          : {result['pipeline']}")
    if result.get("json_path"):
        typer.echo(f"JSON sidecar      : {result['json_path']}")
        typer.echo(f"Text sidecar      : {result['plain_text_path']}")
        typer.echo(f"Tables sidecar    : {result['tables_path']}")
    else:
        typer.echo("Storage           : db")


@doc_intel_app.command("benchmark-docling")
def benchmark_docling(
    pdf_paths: list[str] = typer.Option(
        ..., "--pdf", help="PDF file path(s) to benchmark. Repeat for multiple files."
    ),
    categories: list[str] = typer.Option(
        ..., "--category", help="Category for each PDF: A=native-text, B=rider-table, C=scanned, D=large, E=complex-table."
    ),
    accelerator: str = typer.Option(
        "auto",
        help="Force accelerator: auto (dispatch decides), cpu, or cuda.",
    ),
    output_json: str = typer.Option("", help="Optional path to write JSON results."),
) -> None:
    """Benchmark Docling CPU vs GPU conversion on representative NCUC documents.

    Run each PDF through triage + dispatch + Docling conversion (force=True to bypass cache),
    then print a timing/quality report.

    Example — compare CPU vs GPU on the same file:

      python -m duke_rates doc-intel benchmark-docling \\
          --pdf data/raw/nc/.../leaf-600.pdf --category B \\
          --accelerator cpu

      python -m duke_rates doc-intel benchmark-docling \\
          --pdf data/raw/nc/.../leaf-600.pdf --category B \\
          --accelerator cuda
    """
    from duke_rates.benchmark.pipeline_bench import (
        run_single, print_result, CATEGORY_DESCRIPTIONS, VALID_CATEGORIES,
    )
    from duke_rates.hardware.cpu_config import configure_torch_inference, warmup_gpu

    configure_torch_inference()

    if len(pdf_paths) != len(categories):
        typer.echo("ERROR: --pdf and --category counts must match.")
        raise typer.Exit(code=1)

    for cat in categories:
        if cat not in VALID_CATEGORIES:
            typer.echo(f"ERROR: unknown category '{cat}'. Valid: {', '.join(VALID_CATEGORIES)}")
            raise typer.Exit(code=1)

    typer.echo("=== Docling Pipeline Benchmark ===")
    for cat, desc in CATEGORY_DESCRIPTIONS.items():
        typer.echo(f"  {cat}: {desc}")
    typer.echo("")

    if accelerator == "auto":
        accel_arg = None
        typer.echo("Accelerator: auto (dispatch decides per document)")
    else:
        accel_arg = accelerator
        typer.echo(f"Accelerator: forced={accelerator}")

    if accel_arg == "cuda" or accelerator == "auto":
        warmed = warmup_gpu()
        if warmed:
            typer.echo("GPU warmed up.")

    results = []
    for pdf_path, category in zip(pdf_paths, categories):
        typer.echo(f"\nProcessing: {pdf_path}  (cat={category})")
        r = run_single(pdf_path, category, accelerator=accel_arg)
        print_result(r)
        results.append(r.as_dict())

    # Summary table
    typer.echo("\n=== Summary ===")
    typer.echo(f"{'File':<40} {'Cat':>3} {'Accel':>5} {'Pages':>5} {'Conv(s)':>8} {'p/s':>6} {'Tables':>6}")
    typer.echo("-" * 80)
    for r in results:
        name = Path(r["pdf_path"]).name[:38]
        typer.echo(
            f"{name:<40} {r['category']:>3} {r['accelerator_used']:>5} "
            f"{r['page_count']:>5} {r['conversion_time_s']:>8.2f} "
            f"{r['pages_per_second']:>6.2f} {r['tables_detected']:>6}"
        )

    if output_json:
        import json as _json
        Path(output_json).write_text(_json.dumps(results, indent=2), encoding="utf-8")
        typer.echo(f"\nResults written to: {output_json}")


@doc_intel_app.command("benchmark-document-normalization")
def benchmark_document_normalization(
    pdf_paths: list[str] = typer.Option(
        ..., "--pdf", help="PDF file path(s) to benchmark. Repeat for multiple files."
    ),
    labels: list[str] = typer.Option(
        ..., "--label", help="Short label for each PDF benchmark case."
    ),
    max_pages: int = typer.Option(
        2,
        help="Maximum number of leading pages to benchmark for each PDF.",
    ),
    skip_glm: bool = typer.Option(
        False,
        help="Skip GLM-OCR comparison and router fallback to GLM.",
    ),
    ollama_host: str = typer.Option(
        "http://localhost:11434",
        help="Local Ollama host for GLM-OCR benchmarking.",
    ),
    output_json: str = typer.Option("", help="Optional path to write JSON results."),
) -> None:
    """Benchmark native vs Paddle vs GLM document normalization on representative PDFs."""
    from duke_rates.benchmark.document_normalization_bench import (
        print_normalization_benchmark,
        run_normalization_benchmark,
        write_results_json,
    )

    if len(pdf_paths) != len(labels):
        typer.echo("ERROR: --pdf and --label counts must match.")
        raise typer.Exit(code=1)

    typer.echo("=== Document Normalization Benchmark ===")
    typer.echo(f"max_pages={max_pages}  glm_enabled={not skip_glm}  ollama_host={ollama_host}")

    results: list[dict] = []
    for pdf_path, label in zip(pdf_paths, labels):
        typer.echo(f"\nProcessing: {label} -> {pdf_path}")
        result = run_normalization_benchmark(
            pdf_path,
            label=label,
            max_pages=max_pages,
            enable_glm=not skip_glm,
            ollama_host=ollama_host,
        )
        print_normalization_benchmark(result)
        results.append(result)

    if output_json:
        write_results_json(results, output_json)
        typer.echo(f"\nResults written to: {output_json}")


@doc_intel_app.command("compare-document-page-text")
def compare_document_page_text(
    pdf_paths: list[str] = typer.Option(
        ..., "--pdf", help="PDF file path(s) to compare. Repeat for multiple cases."
    ),
    pages: list[int] = typer.Option(
        ..., "--page", help="1-based page number for each PDF case."
    ),
    labels: list[str] = typer.Option(
        ..., "--label", help="Short label for each comparison case."
    ),
    expected_tokens: list[str] = typer.Option(
        [],
        "--expected-token",
        help="Expected token(s) that indicate better OCR accuracy. Repeat as needed.",
    ),
    skip_glm: bool = typer.Option(
        False,
        help="Skip GLM-OCR comparison.",
    ),
    skip_paddle: bool = typer.Option(
        False,
        help="Skip Paddle comparison.",
    ),
    ollama_host: str = typer.Option(
        "http://localhost:11434",
        help="Local Ollama host for GLM-OCR comparison.",
    ),
    output_json: str = typer.Option("", help="Optional path to write JSON results."),
    output_markdown: str = typer.Option("", help="Optional path to write Markdown results."),
) -> None:
    """Compare page-level text accuracy across native, Paddle, and GLM OCR backends."""
    from duke_rates.benchmark.document_page_text_compare import (
        print_document_page_text_comparison,
        run_document_page_text_comparison,
        write_page_comparison_json,
        write_page_comparison_markdown,
    )

    if not (len(pdf_paths) == len(pages) == len(labels)):
        typer.echo("ERROR: --pdf, --page, and --label counts must match.")
        raise typer.Exit(code=1)

    typer.echo("=== Document Page Text Comparison ===")
    typer.echo(
        f"glm_enabled={not skip_glm}  paddle_enabled={not skip_paddle}  ollama_host={ollama_host}"
    )
    if expected_tokens:
        typer.echo(f"expected_tokens={expected_tokens}")

    results: list[dict] = []
    for pdf_path, page, label in zip(pdf_paths, pages, labels):
        typer.echo(f"\nProcessing: {label} -> {pdf_path} (page {page})")
        result = run_document_page_text_comparison(
            pdf_path,
            page_number=page,
            label=label,
            expected_tokens=expected_tokens,
            enable_glm=not skip_glm,
            enable_paddle=not skip_paddle,
            ollama_host=ollama_host,
        )
        print_document_page_text_comparison(result)
        results.append(result)

    if output_json:
        write_page_comparison_json(results, output_json)
        typer.echo(f"\nJSON results written to: {output_json}")
    if output_markdown:
        write_page_comparison_markdown(results, output_markdown)
        typer.echo(f"Markdown results written to: {output_markdown}")


@doc_intel_app.command("benchmark-redline-analysis")
def benchmark_redline_analysis(
    pdf_paths: list[str] = typer.Option(
        ..., "--pdf", help="PDF file path(s) to analyze. Repeat for multiple cases."
    ),
    pages: list[int] = typer.Option(
        ..., "--page", help="1-based page number for each case."
    ),
    labels: list[str] = typer.Option(
        ..., "--label", help="Short label for each case."
    ),
    ollama_host: str = typer.Option(
        "http://localhost:11434",
        help="Local Ollama host for GLM redline analysis.",
    ),
    output_json: str = typer.Option("", help="Optional path to write JSON results."),
    output_markdown: str = typer.Option("", help="Optional path to write Markdown results."),
) -> None:
    """Benchmark GLM image analysis on candidate clean/redline tariff pages."""
    from duke_rates.benchmark.redline_analysis_bench import (
        print_redline_analysis,
        run_redline_analysis,
        write_redline_analysis_json,
        write_redline_analysis_markdown,
    )

    if not (len(pdf_paths) == len(pages) == len(labels)):
        typer.echo("ERROR: --pdf, --page, and --label counts must match.")
        raise typer.Exit(code=1)

    typer.echo("=== Redline Analysis Benchmark ===")
    typer.echo(f"ollama_host={ollama_host}")

    results: list[dict] = []
    for pdf_path, page, label in zip(pdf_paths, pages, labels):
        typer.echo(f"\nProcessing: {label} -> {pdf_path} (page {page})")
        result = run_redline_analysis(
            pdf_path,
            page_number=page,
            label=label,
            ollama_host=ollama_host,
        )
        print_redline_analysis(result)
        results.append(result)

    if output_json:
        write_redline_analysis_json(results, output_json)
        typer.echo(f"\nJSON results written to: {output_json}")
    if output_markdown:
        write_redline_analysis_markdown(results, output_markdown)
        typer.echo(f"Markdown results written to: {output_markdown}")


@doc_intel_app.command("report-docling-skipped-pages")
def report_docling_skipped_pages_nc(
    limit: int = typer.Option(50, "--limit", help="Maximum number of artifacts to list."),
    min_skipped: int = typer.Option(1, "--min-skipped", min=1, help="Only include artifacts with at least N skipped pages."),
    degraded_only: bool = typer.Option(False, "--degraded-only", help="Only include artifacts that triggered the per-page degradation ladder."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full report as JSON."),
) -> None:
    """List Docling artifacts where pages were skipped or degraded during chunked conversion.

    Reads ``metadata_json`` written by ``convert_pdf_safe`` and surfaces:
      * ``skipped_pages``     — pages that exhausted the degradation ladder
      * ``degraded_modes``    — labels like ``page_degraded``, ``cpu_fallback``, ``chunked``
      * ``used_chunking``     — whether page-range chunking was used at all

    These pages are candidates for VLM (``doc-intel run-docling-vlm``) or manual remediation —
    Docling's standard pipeline could not produce text for them.
    """
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        rows = conn.execute(
            """
            SELECT id, source_pdf, accelerator, pipeline, status, page_count,
                   table_count, metadata_json, updated_at
            FROM docling_artifacts
            WHERE metadata_json IS NOT NULL AND metadata_json != '{}'
            ORDER BY id DESC
            """
        ).fetchall()
    finally:
        conn.close()

    matches: list[dict] = []
    skipped_total = 0
    degraded_total = 0
    chunked_total = 0
    for row in rows:
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            continue
        skipped_pages = list(meta.get("skipped_pages") or [])
        degraded_modes = list(meta.get("degraded_modes") or [])
        used_chunking = bool(meta.get("used_chunking"))
        if not (skipped_pages or degraded_modes or used_chunking):
            continue
        if len(skipped_pages) < min_skipped and not degraded_modes:
            # Still tally totals before filtering, but don't include in rows.
            if used_chunking:
                chunked_total += 1
            continue
        if degraded_only and not degraded_modes:
            continue
        skipped_total += len(skipped_pages)
        if degraded_modes:
            degraded_total += 1
        if used_chunking:
            chunked_total += 1
        matches.append(
            {
                "id": row["id"],
                "source_pdf": row["source_pdf"],
                "accelerator": row["accelerator"],
                "pipeline": row["pipeline"],
                "status": row["status"],
                "page_count": row["page_count"],
                "table_count": row["table_count"],
                "skipped_pages": skipped_pages,
                "skipped_count": len(skipped_pages),
                "degraded_modes": degraded_modes,
                "used_chunking": used_chunking,
                "updated_at": row["updated_at"],
            }
        )

    matches.sort(key=lambda m: m["skipped_count"], reverse=True)
    truncated = matches[:limit]

    report = {
        "row_count": len(matches),
        "shown": len(truncated),
        "skipped_pages_total": skipped_total,
        "degraded_artifacts_total": degraded_total,
        "chunked_artifacts_total": chunked_total,
        "rows": truncated,
    }

    if as_json:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo(f"artifacts_with_issues={report['row_count']} shown={report['shown']}")
    typer.echo(f"  skipped_pages_total={report['skipped_pages_total']}")
    typer.echo(f"  degraded_artifacts={report['degraded_artifacts_total']}")
    typer.echo(f"  chunked_artifacts={report['chunked_artifacts_total']}")
    if not truncated:
        typer.echo("(no matching artifacts)")
        return
    typer.echo("Top rows (by skipped page count):")
    for row in truncated:
        typer.echo(
            f"  id={row['id']} "
            f"pages={row['page_count']} "
            f"skipped={row['skipped_count']} "
            f"deg={','.join(row['degraded_modes']) or 'none'} "
            f"chunked={row['used_chunking']} "
            f"src={row['source_pdf']}"
        )


@doc_intel_app.command("list-document-types")
def list_document_types_nc(
    as_json: bool = typer.Option(False, "--json", help="Emit the taxonomy as JSON."),
) -> None:
    """List the seeded ``document_types`` taxonomy.

    Phase 2 of the document intelligence roadmap. Use this to confirm the
    taxonomy is populated before relying on the ``document_type``
    classification stage.
    """
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        rows = conn.execute(
            """
            SELECT code, primary_category, parent_type, description, is_terminal
            FROM document_types
            ORDER BY primary_category, code
            """
        ).fetchall()
    finally:
        conn.close()

    items = [
        {
            "code": r["code"],
            "primary_category": r["primary_category"],
            "parent_type": r["parent_type"],
            "description": r["description"],
            "is_terminal": bool(r["is_terminal"]),
        }
        for r in rows
    ]

    if as_json:
        typer.echo(json.dumps(items, indent=2))
        return

    if not items:
        typer.echo("(no document_types rows — run any DB migrate to seed)")
        return

    current_category = None
    for item in items:
        if item["primary_category"] != current_category:
            current_category = item["primary_category"]
            typer.echo("")
            typer.echo(current_category)
        typer.echo(f"  {item['code']:<28s} {item['description']}")


@doc_intel_app.command("report-document-types")
def report_document_types_nc(
    as_json: bool = typer.Option(False, "--json", help="Emit the report as JSON."),
) -> None:
    """Report the distribution of ``document_type`` classifications.

    Phase 2 definition-of-done check: a non-trivial spread across the
    seeded types (not 100% UNKNOWN) means the live classifier is wired
    and the taxonomy fits the corpus. Compares against the legacy
    ``classify_document`` string label captured in metadata so disagreements
    are visible at a glance.
    """
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        distribution_rows = conn.execute(
            """
            SELECT label, COUNT(*) AS n
            FROM document_classifications
            WHERE stage = 'document_type' AND superseded_by IS NULL
            GROUP BY label
            ORDER BY n DESC
            """
        ).fetchall()
        confidence_rows = conn.execute(
            """
            SELECT
                ROUND(confidence, 1) AS bucket,
                COUNT(*) AS n
            FROM document_classifications
            WHERE stage = 'document_type' AND superseded_by IS NULL
            GROUP BY bucket
            ORDER BY bucket
            """
        ).fetchall()
        legacy_rows = conn.execute(
            """
            SELECT label, metadata_json, COUNT(*) AS n
            FROM document_classifications
            WHERE stage = 'document_type' AND superseded_by IS NULL
            GROUP BY label, metadata_json
            """
        ).fetchall()
    finally:
        conn.close()

    distribution = [{"label": r["label"], "count": r["n"]} for r in distribution_rows]
    total = sum(item["count"] for item in distribution)
    confidence_buckets = [
        {"bucket": r["bucket"], "count": r["n"]} for r in confidence_rows
    ]

    legacy_xref: dict[str, dict[str, int]] = {}
    for row in legacy_rows:
        try:
            md = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            md = {}
        legacy = str(md.get("legacy_label", "<missing>"))
        legacy_xref.setdefault(row["label"], {}).setdefault(legacy, 0)
        legacy_xref[row["label"]][legacy] += int(row["n"])

    report = {
        "stage": "document_type",
        "total": total,
        "distribution": distribution,
        "confidence_buckets": confidence_buckets,
        "legacy_label_crosswalk": legacy_xref,
    }

    if as_json:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo(f"stage=document_type total={total}")
    if total == 0:
        typer.echo("  (no rows — run extraction to populate)")
        return

    typer.echo("")
    typer.echo("Distribution by document_type:")
    for item in distribution:
        pct = 100.0 * item["count"] / total if total else 0.0
        typer.echo(f"  {item['label']:<28s} {item['count']:>6,}  ({pct:5.1f}%)")

    typer.echo("")
    typer.echo("Confidence distribution (rounded to 0.1 buckets):")
    for bucket in confidence_buckets:
        typer.echo(f"  conf~{bucket['bucket']}  {bucket['count']:>6,}")

    typer.echo("")
    typer.echo("Crosswalk to legacy classify_document label:")
    for dt_label in sorted(legacy_xref):
        legacy_map = legacy_xref[dt_label]
        legacy_str = ", ".join(
            f"{legacy}={count}" for legacy, count in sorted(legacy_map.items())
        )
        typer.echo(f"  {dt_label:<28s} <- {legacy_str}")


@doc_intel_app.command("report-flag-classifications")
def report_flag_classifications_nc(
    stage: str = typer.Option("", "--stage", help="Filter to a specific flag stage (e.g. 'flag_is_final'). Empty = all."),
    as_json: bool = typer.Option(False, "--json", help="Emit the report as JSON."),
) -> None:
    """Report the distribution of Phase 3 flag classifications.

    Shows per-stage label distributions and confidence ranges, including
    how many documents have been classified for each flag stage.
    """
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        stages = conn.execute(
            """
            SELECT DISTINCT stage
            FROM document_classifications
            WHERE stage LIKE 'flag_%' OR stage IN ('utility', 'docket_number', 'effective_date', 'tariff_family')
            ORDER BY stage
            """
        ).fetchall()
    finally:
        conn.close()

    stage_list = [s["stage"] for s in stages]
    if stage:
        if stage not in stage_list:
            typer.echo(f"No data for stage {stage!r}. Available: {stage_list}")
            raise typer.Exit(code=0)
        stage_list = [stage]

    if not stage_list:
        typer.echo("(no flag classification rows — run extraction to populate)")
        return

    report: dict[str, dict] = {}
    for st in stage_list:
        conn = connect(settings.database_path)
        try:
            dist = conn.execute(
                """
                SELECT label, COUNT(*) AS n
                FROM document_classifications
                WHERE stage = ? AND superseded_by IS NULL
                GROUP BY label ORDER BY n DESC
                """,
                (st,),
            ).fetchall()
            conf = conn.execute(
                """
                SELECT MIN(confidence) AS mn, MAX(confidence) AS mx, AVG(confidence) AS avg
                FROM document_classifications
                WHERE stage = ? AND superseded_by IS NULL
                """,
                (st,),
            ).fetchone()
            total = conn.execute(
                "SELECT COUNT(*) FROM document_classifications WHERE stage = ? AND superseded_by IS NULL",
                (st,),
            ).fetchone()[0]
        finally:
            conn.close()
        report[st] = {
            "total": total or 0,
            "distribution": [{"label": r["label"], "count": r["n"]} for r in dist],
            "confidence": {"min": conf["mn"], "max": conf["mx"], "avg": round(conf["avg"] or 0, 3)},
        }

    if as_json:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    for st, data in sorted(report.items()):
        if data["total"] == 0:
            continue
        typer.echo(f"\n{st}  (n={data['total']})")
        typer.echo(f"  confidence: min={data['confidence']['min']:.2f} max={data['confidence']['max']:.2f} avg={data['confidence']['avg']:.2f}")
        for item in data["distribution"]:
            pct = 100.0 * item["count"] / data["total"] if data["total"] else 0.0
            typer.echo(f"  {item['label']:<20s} {item['count']:>6,}  ({pct:5.1f}%)")


@doc_intel_app.command("backfill-flag-classifications")
def backfill_flag_classifications_nc(
    limit: int = typer.Option(0, "--limit", help="Only process N documents (0 = all)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Classify but do not persist."),
) -> None:
    """Backfill Phase 3 flag classifications for existing historical_documents.

    Extracts text from each document's PDF (first 5000 chars) and runs all
    11 flag classifiers. Skips documents that already have flag rows.
    """
    from duke_rates.classification.persistence import record_classification
    from duke_rates.document_intelligence.flag_classifiers import (
        get_flag_classifier,
        all_flag_stages,
    )
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()

    conn = connect(settings.database_path)
    try:
        rows = conn.execute(
            """
            SELECT hd.*
            FROM historical_documents hd
            WHERE hd.local_path IS NOT NULL AND hd.local_path != ''
              AND hd.id NOT IN (
                  SELECT DISTINCT CAST(subject_id AS INTEGER)
                  FROM document_classifications
                  WHERE subject_kind = 'historical_document'
                    AND (stage LIKE 'flag_%' OR stage IN ('utility', 'docket_number', 'effective_date', 'tariff_family'))
                    AND superseded_by IS NULL
              )
            ORDER BY hd.id
            """
        ).fetchall()
    finally:
        conn.close()

    docs = [dict(r) for r in rows]
    if limit > 0:
        docs = docs[:limit]

    typer.echo(f"Backfilling flag classifications for {len(docs)} documents...")

    if dry_run:
        typer.echo("[DRY RUN — no rows will be written]")

    stages = all_flag_stages()
    ok = skip = fail = 0
    for i, doc in enumerate(docs):
        doc_id = doc.get("id")
        local_path = doc.get("local_path", "")
        if not local_path or not Path(local_path).exists():
            skip += 1
            continue

        try:
            import pdfplumber
            with pdfplumber.open(local_path) as pdf:
                pages = pdf.pages[:3]  # first 3 pages sufficient for classifiers
                text = "\n".join(
                    (p.extract_text() or "") for p in pages
                )
        except Exception:
            fail += 1
            continue

        if not text.strip():
            skip += 1
            continue

        metadata = {
            "company": doc.get("company", ""),
            "family_key": doc.get("family_key", ""),
            "leaf_no": doc.get("leaf_no", ""),
            "effective_start": doc.get("effective_start", ""),
            "docket_number": doc.get("docket_number", ""),
            "is_redline_candidate": int(doc.get("is_redline_candidate") or 0),
            "redline_confidence": float(doc.get("redline_confidence") or 0.0),
            "title": doc.get("title", ""),
        }

        if dry_run:
            for stage in stages:
                classifier = get_flag_classifier(stage)
                if classifier:
                    result = classifier.classify(text, metadata)
            ok += 1
        else:
            cls_conn = connect(settings.database_path)
            try:
                for stage in stages:
                    classifier = get_flag_classifier(stage)
                    if classifier is None:
                        continue
                    result = classifier.classify(text, metadata)
                    result.classifier = f"rule_{stage}_v1"
                    result.classifier_version = "v1"
                    record_classification(
                        cls_conn,
                        subject_kind="historical_document",
                        subject_id=str(doc_id),
                        stage=stage,
                        result=result,
                    )
                cls_conn.commit()
                ok += 1
            except Exception:
                fail += 1
            finally:
                cls_conn.close()

        if (i + 1) % 100 == 0:
            typer.echo(f"  {i + 1}/{len(docs)} ... ok={ok} skip={skip} fail={fail}")

    typer.echo(f"\nDone: ok={ok} skip={skip} fail={fail}")


@doc_intel_app.command("check-ollama-models")
def check_ollama_models_nc(
    config_path: str = typer.Option(
        "config/ollama_models.yaml", "--config", help="Path to ollama_models.yaml"
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit status as JSON."),
    required_only: bool = typer.Option(
        True, "--required-only/--all", help="Only show roles with primary set (--required-only) or all roles (--all)."
    ),
) -> None:
    """Probe every configured Ollama role and report availability.

    Phase 2.5 health check. For each role in ollama_models.yaml, probes the
    primary model (and fallbacks if needed) and reports whether it is available.
    Exits non-zero if any role with a non-empty primary model is unavailable.

    Use this before ``doc-intel run-overnight`` to confirm all
    required models are reachable.
    """
    from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator

    orchestrator = OllamaOrchestrator(config_path=Path(config_path) if config_path else None)
    health = orchestrator.list_available_roles()

    if required_only:
        health = [h for h in health if h.primary]

    if as_json:
        items = [
            {
                "role": h.role,
                "available": h.available,
                "primary": h.primary,
                "message": h.message,
            }
            for h in health
        ]
        typer.echo(json.dumps(items, indent=2))
    else:
        if not health:
            typer.echo("(no roles configured)")
            return

        width_role = max(len(h.role) for h in health) + 2
        width_model = max(len(h.primary) for h in health) + 2
        typer.echo(f"{'ROLE':<{width_role}} {'MODEL':<{width_model}} STATUS")
        typer.echo(f"{'─' * (width_role - 1):<{width_role}} {'─' * (width_model - 1):<{width_model}} ──────")
        for h in health:
            status = "OK" if h.available else f"FAIL — {h.message or 'unknown'}"
            typer.echo(f"{h.role:<{width_role}} {h.primary:<{width_model}} {status}")

    unavailable = [h for h in health if not h.available]
    if unavailable:
        raise typer.Exit(code=1)


@doc_intel_app.command("benchmark-ollama-roles")
def benchmark_ollama_roles_nc(
    task: str = typer.Option(
        "parse_diagnosis",
        "--task",
        help=(
            "Benchmark task, comma-separated tasks, or all. Tasks: "
            "parse_diagnosis, hard_parse_diagnosis, regex_suggestion, "
            "structured_rate_extraction, staged_find_lines, "
            "staged_classify_line, document_classification."
        ),
    ),
    models: str = typer.Option(
        "",
        "--models",
        help="Comma-separated Ollama model names. Defaults to the configured role primary plus fallbacks.",
    ),
    limit: int = typer.Option(5, "--limit", help="Representative cases per task."),
    max_runtime_minutes: float = typer.Option(
        0.0,
        "--max-runtime-minutes",
        help="Stop after this many minutes. 0 means no explicit runtime cap.",
    ),
    timeout_s: float = typer.Option(
        0.0,
        "--timeout-s",
        help="Per-request timeout. 0 uses the role/config default.",
    ),
    config_path: str = typer.Option(
        "config/ollama_models.yaml",
        "--config",
        help="Path to ollama_models.yaml.",
    ),
    output: str = typer.Option(
        "",
        "--output",
        help="Report JSON path. Defaults to docs/reports/ollama_model_benchmarks/<timestamp>_<task>.json.",
    ),
    fixtures: str = typer.Option(
        "",
        "--fixtures",
        help="Optional JSON gold-fixture file with expected labels keyed by task and case_id.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the full benchmark report as JSON."),
) -> None:
    """Benchmark local Ollama models against document-intelligence tasks.

    The benchmark uses production-style prompts and Pydantic schemas but does
    not write diagnostic, suggestion, classification, or extraction rows. It is
    intended for selecting per-role models before overnight loops depend on
    them.
    """
    from duke_rates.document_intelligence.model_benchmark import (
        default_output_path,
        normalize_task_list,
        run_ollama_role_benchmark,
        run_ollama_specialization_benchmark,
    )

    settings, _ = _bootstrap()
    try:
        task_keys = normalize_task_list([task])
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    model_list = [m.strip() for m in models.split(",") if m.strip()] or None
    report_task_name = "all" if len(task_keys) > 1 else task_keys[0]
    output_path = Path(output) if output else default_output_path(report_task_name)
    fixtures_path = Path(fixtures) if fixtures else None

    if len(task_keys) == 1:
        report = run_ollama_role_benchmark(
            db_path=Path(settings.database_path),
            task=task_keys[0],
            models=model_list,
            limit=limit,
            max_runtime_minutes=max_runtime_minutes if max_runtime_minutes > 0 else None,
            config_path=Path(config_path) if config_path else None,
            output_path=output_path,
            timeout_s=timeout_s if timeout_s > 0 else None,
            fixtures_path=fixtures_path,
        )
    else:
        report = run_ollama_specialization_benchmark(
            db_path=Path(settings.database_path),
            tasks=task_keys,
            models=model_list,
            limit=limit,
            max_runtime_minutes=max_runtime_minutes if max_runtime_minutes > 0 else None,
            config_path=Path(config_path) if config_path else None,
            output_path=output_path,
            timeout_s=timeout_s if timeout_s > 0 else None,
            fixtures_path=fixtures_path,
        )

    if as_json:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo("=== Ollama Role Benchmark ===")
    typer.echo(f"Task:          {report['task']}")
    if "role" in report:
        typer.echo(f"Role:          {report['role']}")
    if "tasks" in report:
        typer.echo(f"Tasks:         {', '.join(report['tasks'])}")
    if "cases_selected" in report:
        typer.echo(f"Cases:         {report['cases_selected']}")
    if report.get("gold_case_count"):
        typer.echo(f"Gold cases:    {report['gold_case_count']}")
    typer.echo(f"Runs:          {report['runs_completed']}")
    typer.echo(f"Stop reason:   {report['stop_reason']}")
    typer.echo(f"Report:        {output_path}")
    typer.echo("")

    if report.get("task") == "multi_task_specialization":
        specialization = report.get("specialization") or {}
        typer.echo("Best by task:")
        for task_name, row in (specialization.get("best_by_task") or {}).items():
            if not row:
                continue
            typer.echo(
                f"  {task_name:<28s} {row['model']:<28.28s} "
                f"score={row['score']:.1f} valid={row['valid_pct']:.1f}% "
                f"action={row['actionable_pct']:.1f}% bias={row['label_bias_score']:.2f}"
            )
        typer.echo("")
        return

    typer.echo(
        f"{'MODEL':<28s} {'OK':>5s} {'VALID%':>8s} {'ACTION%':>8s} "
        f"{'AVG S':>8s} {'TPS':>8s} {'CONF':>7s} {'BIAS':>7s} {'ACC%':>7s}"
    )
    typer.echo("-" * 98)
    for model, stats in report["summary"].items():
        avg_s = float(stats.get("avg_duration_ms", 0.0)) / 1000.0
        typer.echo(
            f"{model:<28.28s} "
            f"{stats.get('ok', 0):>5d} "
            f"{stats.get('valid_pct', 0.0):>7.1f}% "
            f"{stats.get('actionable_pct', 0.0):>7.1f}% "
            f"{avg_s:>8.1f} "
            f"{stats.get('avg_tokens_per_second', 0.0):>8.1f} "
            f"{stats.get('avg_confidence', 0.0):>7.2f} "
            f"{stats.get('label_bias_score', 0.0):>7.2f} "
            f"{_format_optional_pct(stats.get('accuracy_pct')):>7s}"
        )
        distribution = stats.get("task_distribution") or {}
        if distribution:
            typer.echo(f"  distribution: {distribution}")


@doc_intel_app.command("run-llm-doc-probe")
def run_llm_doc_probe_nc(
    document_id: int = typer.Argument(..., help="historical_documents.id to probe."),
    role: str = typer.Option("balanced_classifier", "--role", help="Ollama role from ollama_models.yaml."),
    config_path: str = typer.Option("config/ollama_models.yaml", "--config", help="Path to ollama_models.yaml."),
    persist: bool = typer.Option(False, "--persist", help="Write classification to document_classifications."),
    as_json: bool = typer.Option(False, "--json", help="Emit result as JSON."),
) -> None:
    """Run an LLM probe against one historical document.

    Phase 2.5 smoke-test entrypoint. Extracts text from *document_id*, sends it
    to *role*'s primary model in JSON mode, validates the output against a
    light-weight document-type schema, and prints the result.

    Does NOT write to document_classifications unless ``--persist`` is passed.
    The ollama_model_runs row is always persisted.
    """
    from duke_rates.classification.result import ClassificationResult
    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        doc = conn.execute(
            "SELECT id, title, family_key, local_path, raw_text_path FROM historical_documents WHERE id = ?",
            (document_id,),
        ).fetchone()
    finally:
        conn.close()

    if doc is None:
        raise typer.BadParameter(f"No historical_document with id={document_id}")

    # Extract text
    text_sample = ""
    if doc["raw_text_path"]:
        try:
            text_sample = Path(doc["raw_text_path"]).read_text(encoding="utf-8")[:2000]
        except Exception:
            pass
    if not text_sample and doc["local_path"]:
        from duke_rates.parse.pdf import extract_pdf_text
        try:
            text_sample = extract_pdf_text(doc["local_path"])[:2000]
        except Exception:
            text_sample = "[text extraction failed]"

    orchestrator = OllamaOrchestrator(
        config_path=Path(config_path) if config_path else None,
        db_path=settings.database_path,
    )

    # Probe the role first
    ok, err = orchestrator.health_probe(role)
    if not ok:
        typer.echo(f"Role {role!r} not available: {err}")
        raise typer.Exit(code=1)

    prompt = (
        "Classify this NCUC regulatory document into exactly one of these types: "
        "TARIFF_SHEET, RIDER, RATE_SCHEDULE, ORDER_FINAL, ORDER_PROCEDURAL, "
        "TESTIMONY, COVER_LETTER, NOTICE_OF_HEARING, APPLICATION, "
        "COMPLIANCE_FILING, CERTIFICATE_OF_SERVICE, UNKNOWN.\n\n"
        "Return JSON with fields: label (the type), confidence (0.0-1.0), "
        "evidence (list of {kind, value} objects), "
        "alternatives (list of [label, score] pairs).\n\n"
        f"Document title: {doc['title']}\n"
        f"Document text (first 2000 chars):\n{text_sample}"
    )

    result = orchestrator.generate_json(
        role=role,
        prompt=prompt,
        schema=ClassificationResult,
        subject_kind="historical_document",
        subject_id=str(document_id),
        stage="llm_probe",
    )

    if as_json:
        output = {
            "document_id": document_id,
            "title": doc["title"],
            "family_key": doc["family_key"],
            "role": result.role,
            "model": result.model,
            "status": result.status,
            "duration_ms": result.duration_ms,
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "result": result.result.model_dump() if result.result else None,
            "raw_payload": result.raw_payload,
            "validation_error": result.validation_error,
            "fallback_from": result.fallback_from,
        }
        typer.echo(json.dumps(output, indent=2, default=str))
    else:
        typer.echo(f"Document: {doc['id']} ({doc['family_key']})")
        typer.echo(f"Title:    {doc['title'][:100]}")
        typer.echo(f"Role:     {result.role} -> {result.model}")
        typer.echo(f"Status:   {result.status}")
        typer.echo(f"Duration: {result.duration_ms} ms")
        typer.echo(f"Tokens:   in={result.tokens_in} out={result.tokens_out}")
        if result.fallback_from:
            typer.echo(f"Fallback: {result.fallback_from} -> {result.model}")
        if result.validation_error:
            typer.echo(f"Validation error: {result.validation_error}")
        if result.result:
            typer.echo(f"Label:      {result.result.label}")
            typer.echo(f"Confidence: {result.result.confidence}")
            if result.result.alternatives:
                typer.echo(f"Alternatives: {result.result.alternatives}")
        if result.raw_payload:
            typer.echo(f"\nRaw response:\n{result.raw_payload[:500]}")


@doc_intel_app.command("report-classification-disagreements")
def report_classification_disagreements_nc(
    stage: str = typer.Option("family_mapping", "--stage", help="Classification stage to inspect."),
    margin: float = typer.Option(0.10, "--margin", help="Confidence margin between rank-1 and rank-2 below which a row is flagged as 'low margin'."),
    limit: int = typer.Option(50, "--limit", help="Max rows to show."),
    overrides_only: bool = typer.Option(False, "--overrides-only", help="Only show rows where a hint/override changed the classifier's chosen label."),
    as_json: bool = typer.Option(False, "--json", help="Emit the full report as JSON."),
    cross_stage: str = typer.Option("", "--cross-stage", help="Compare two classifiers for the same stage (e.g. 'document_type'). Lists (rule vs embedding) pairs per document."),
) -> None:
    """Surface low-confidence and runner-up-close classifications.

    Reads ``document_classifications`` and reports:
      * **Low-margin** rows — rank-1 score and rank-2 score are within ``--margin`` (raw score units, not confidence). These are where the classifier was on the edge between two labels and small changes in evidence would have flipped the decision.
      * **Override** rows — the legacy hint (or other override source) chose a label different from what the classifier picked.
      * **Cross-stage** comparison (``--cross-stage document_type``) — compares rule-based vs embedding-based classifications for the same document.

    Use this to triage which family-mapping decisions to review by hand or
    feed to a second-opinion classifier.
    """
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()

    # ------------------------------------------------------------------
    # Cross-stage comparison path (Phase 4)
    # ------------------------------------------------------------------
    if cross_stage:
        conn = connect(settings.database_path)
        try:
            pairs = conn.execute(
                """
                SELECT
                    r.subject_kind,
                    r.subject_id,
                    r.label AS rule_label,
                    r.confidence AS rule_confidence,
                    e.label AS emb_label,
                    e.confidence AS emb_confidence,
                    CASE
                        WHEN r.label = e.label THEN 'agreement'
                        WHEN r.confidence < 0.3 AND e.confidence >= 0.5 AND r.label = e.label
                            THEN 'embedding_confirms_weak_rule'
                        WHEN r.confidence < 0.3 AND e.confidence >= 0.5 AND r.label != e.label
                            THEN 'overrule_candidate'
                        WHEN r.label != e.label THEN 'disagreement'
                        ELSE 'other'
                    END AS status
                FROM document_classifications r
                JOIN document_classifications e
                  ON e.subject_kind = r.subject_kind
                 AND e.subject_id = r.subject_id
                 AND e.stage = r.stage
                 AND e.classifier = 'embedding_knn_v1'
                 AND e.superseded_by IS NULL
                WHERE r.stage = ?
                  AND r.classifier = 'rule_document_type_v1'
                  AND r.superseded_by IS NULL
                ORDER BY
                    CASE
                        WHEN r.label != e.label THEN 0
                        WHEN r.confidence < 0.3 THEN 1
                        ELSE 2
                    END,
                    ABS(r.confidence - e.confidence) DESC
                """,
                (cross_stage,),
            ).fetchall()
        finally:
            conn.close()

        report = {
            "cross_stage": cross_stage,
            "total_pairs": len(pairs),
            "agreements": sum(1 for p in pairs if p["status"] == "agreement"),
            "disagreements": sum(1 for p in pairs if p["status"] == "disagreement"),
            "overrule_candidates": sum(1 for p in pairs if p["status"] == "overrule_candidate"),
            "embedding_confirms_weak_rule": sum(1 for p in pairs if p["status"] == "embedding_confirms_weak_rule"),
            "pairs": [
                {
                    "subject_kind": p["subject_kind"],
                    "subject_id": p["subject_id"],
                    "rule_label": p["rule_label"],
                    "rule_confidence": round(float(p["rule_confidence"]), 3),
                    "emb_label": p["emb_label"],
                    "emb_confidence": round(float(p["emb_confidence"]), 3),
                    "status": p["status"],
                }
                for p in pairs
            ][:limit],
        }

        if as_json:
            typer.echo(json.dumps(report, indent=2, default=str))
            return

        typer.echo(f"Cross-stage comparison: {cross_stage}")
        typer.echo(f"  total pairs:      {report['total_pairs']}")
        typer.echo(f"  agreements:       {report['agreements']}")
        typer.echo(f"  disagreements:    {report['disagreements']}")
        typer.echo(f"  overrule candidates: {report['overrule_candidates']}")
        typer.echo(f"  embedding confirms weak rule: {report['embedding_confirms_weak_rule']}")

        if report["pairs"]:
            typer.echo("")
            typer.echo(f"{'subj_id':>8s} {'rule_label':<20s} {'r_conf':>6s} {'emb_label':<20s} {'e_conf':>6s} {'status'}")
            typer.echo("-" * 90)
            for p in report["pairs"]:
                typer.echo(
                    f"{p['subject_id']:>8s} "
                    f"{p['rule_label']:<20s} "
                    f"{p['rule_confidence']:>6.3f} "
                    f"{p['emb_label']:<20s} "
                    f"{p['emb_confidence']:>6.3f} "
                    f"{p['status']}"
                )
        return

    conn = connect(settings.database_path)
    try:
        rows = conn.execute(
            """
            SELECT id, subject_kind, subject_id, stage, label, confidence,
                   classifier, classifier_version, evidence_json,
                   alternatives_json, metadata_json, created_at
            FROM document_classifications
            WHERE stage = ? AND superseded_by IS NULL
            ORDER BY id DESC
            """,
            (stage,),
        ).fetchall()
    finally:
        conn.close()

    low_margin: list[dict] = []
    overrides: list[dict] = []
    low_confidence: list[dict] = []
    for row in rows:
        try:
            alternatives = json.loads(row["alternatives_json"] or "[]")
        except (TypeError, ValueError):
            alternatives = []
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}

        is_override = bool(metadata.get("override_source"))
        if is_override:
            overrides.append(
                {
                    "id": row["id"],
                    "subject_kind": row["subject_kind"],
                    "subject_id": row["subject_id"],
                    "chosen_label": row["label"],
                    "classifier_label": metadata.get("classifier_label"),
                    "classifier_confidence": metadata.get("classifier_confidence"),
                    "override_source": metadata.get("override_source"),
                    "alternatives": alternatives[:3],
                }
            )

        # For stages that produce alternatives (e.g. family_mapping),
        # compare rank-1 vs rank-2 scores. For flag/boolean stages,
        # flag rows with low confidence directly.
        is_flag_stage = (
            row["stage"].startswith("flag_")
            or row["stage"] in ("utility", "docket_number", "effective_date", "tariff_family")
        )
        if not is_flag_stage and alternatives:
            chosen_score = float(row["confidence"]) * 118.0  # _MAX_FAMILY_SCORE in family_matcher.py
            runner_score = float(alternatives[0][1]) if alternatives[0] else 0.0
            margin_score = chosen_score - runner_score
            if margin_score < margin * 118.0:
                low_margin.append(
                    {
                        "id": row["id"],
                        "subject_kind": row["subject_kind"],
                        "subject_id": row["subject_id"],
                        "chosen_label": row["label"],
                        "chosen_confidence": round(float(row["confidence"]), 3),
                        "runner_up_label": alternatives[0][0],
                        "runner_up_score": runner_score,
                        "margin_score": round(margin_score, 2),
                    }
                )

        if is_flag_stage:
            conf = float(row["confidence"])
            if conf > 0.0 and conf < 0.5:
                low_confidence.append(
                    {
                        "id": row["id"],
                        "subject_kind": row["subject_kind"],
                        "subject_id": row["subject_id"],
                        "label": row["label"],
                        "confidence": round(conf, 3),
                    }
                )

    report = {
        "stage": stage,
        "total_classifications": len(rows),
        "low_margin_count": len(low_margin),
        "overrides_count": len(overrides),
        "low_confidence_count": len(low_confidence),
        "low_margin": sorted(low_margin, key=lambda r: r["margin_score"])[:limit],
        "overrides": overrides[:limit],
        "low_confidence": sorted(low_confidence, key=lambda r: r["confidence"])[:limit],
    }

    if as_json:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    is_flag_stage = (
        stage.startswith("flag_")
        or stage in ("utility", "docket_number", "effective_date", "tariff_family")
    )
    typer.echo(f"stage={stage}")
    typer.echo(f"  total active classifications: {report['total_classifications']}")
    if is_flag_stage:
        typer.echo(f"  low-confidence (conf < 0.5): {report['low_confidence_count']}")
    else:
        typer.echo(f"  low-margin (rank-1 within {margin} of rank-2): {report['low_margin_count']}")
        typer.echo(f"  overrides (hint changed classifier's label): {report['overrides_count']}")

    if not overrides_only and report["low_margin"]:
        typer.echo("")
        typer.echo("Top low-margin rows:")
        for r in report["low_margin"]:
            typer.echo(
                f"  id={r['id']} subj={r['subject_kind']}/{r['subject_id']} "
                f"chosen={r['chosen_label']} conf={r['chosen_confidence']} "
                f"runner_up={r['runner_up_label']} margin={r['margin_score']}"
            )

    if report["overrides"]:
        typer.echo("")
        typer.echo("Override rows (classifier vs hint disagreement):")
        for r in report["overrides"]:
            typer.echo(
                f"  id={r['id']} subj={r['subject_kind']}/{r['subject_id']} "
                f"chosen={r['chosen_label']} (was: {r['classifier_label']}, "
                f"clf_conf={r['classifier_confidence']}, src={r['override_source']})"
            )

    if report["low_confidence"]:
        typer.echo("")
        typer.echo("Low-confidence rows (conf < 0.5):")
        for r in report["low_confidence"]:
            typer.echo(
                f"  id={r['id']} subj={r['subject_kind']}/{r['subject_id']} "
                f"label={r['label']} conf={r['confidence']:.3f}"
            )


@doc_intel_app.command("adjudicate-classifications")
def adjudicate_classifications_nc(
    limit: int = typer.Option(10, "--limit", help="Max documents to adjudicate."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be adjudicated without calling the LLM."),
    as_json: bool = typer.Option(False, "--json", help="Emit the report as JSON."),
) -> None:
    """Run LLM adjudication on document_type disagreements.

    Finds rows where rule-based and embedding classifiers disagree, or where
    confidence is low (<0.5), or where either returned UNKNOWN. Runs the LLM
    adjudicator (``balanced_classifier`` role) on each and persists a new
    ``document_classifications`` row with classifier ``llm_<model>_v1``.

    The LLM result does NOT auto-supersede rule/embedding rows — superseding
    happens only via Phase 6 human review.
    """
    import sqlite3

    from duke_rates.classification.persistence import record_classification
    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.llm_classifier import LLMAdjudicator
    from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator
    from duke_rates.document_intelligence.text_slicer import slice_pdf_text
    from duke_rates.classification.result import ClassificationResult

    settings, _ = _bootstrap()

    # 1. Find candidate rows
    conn = connect(settings.database_path)
    try:
        candidates = conn.execute(
            """
            SELECT
                r.subject_id,
                r.label AS rule_label,
                r.confidence AS rule_confidence,
                r.evidence_json AS rule_evidence,
                e.label AS emb_label,
                e.confidence AS emb_confidence,
                e.evidence_json AS emb_evidence
            FROM document_classifications r
            JOIN document_classifications e
              ON e.subject_kind = r.subject_kind
             AND e.subject_id = r.subject_id
             AND e.stage = r.stage
             AND e.classifier = 'embedding_knn_v1'
             AND e.superseded_by IS NULL
            LEFT JOIN document_classifications existing_llm
              ON existing_llm.subject_kind = r.subject_kind
             AND existing_llm.subject_id = r.subject_id
             AND existing_llm.stage = r.stage
             AND existing_llm.classifier LIKE 'llm_%'
             AND existing_llm.superseded_by IS NULL
            WHERE r.stage = 'document_type'
              AND r.classifier = 'rule_document_type_v1'
              AND r.superseded_by IS NULL
              AND existing_llm.id IS NULL
              AND (
                  r.label != e.label
                  OR r.label = 'UNKNOWN' OR e.label = 'UNKNOWN'
                  OR MAX(r.confidence, e.confidence) < 0.5
              )
            ORDER BY
                CASE
                    WHEN r.label = 'UNKNOWN' OR e.label = 'UNKNOWN' THEN 0
                    WHEN r.label != e.label THEN 1
                    ELSE 2
                END,
                ABS(r.confidence - e.confidence) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    if not candidates:
        typer.echo("No candidates found for LLM adjudication.")
        return

    typer.echo(f"Found {len(candidates)} candidate(s) for LLM adjudication.")
    typer.echo(f"  disagreements: {sum(1 for c in candidates if c['rule_label'] != c['emb_label'])}")
    typer.echo(f"  UNKNOWN (rule or emb): {sum(1 for c in candidates if c['rule_label'] == 'UNKNOWN' or c['emb_label'] == 'UNKNOWN')}")
    typer.echo(f"  low-confidence: {sum(1 for c in candidates if max(float(c['rule_confidence']), float(c['emb_confidence'])) < 0.5)}")

    if dry_run:
        typer.echo("")
        typer.echo(f"{'subj_id':>8s} {'rule_label':<20s} {'r_conf':>6s} {'emb_label':<20s} {'e_conf':>6s}")
        typer.echo("-" * 72)
        for c in candidates:
            typer.echo(
                f"{c['subject_id']:>8s} "
                f"{c['rule_label']:<20s} "
                f"{float(c['rule_confidence']):>6.3f} "
                f"{c['emb_label']:<20s} "
                f"{float(c['emb_confidence']):>6.3f}"
            )
        return

    # 2. Initialize adjudicator
    orch = OllamaOrchestrator(db_path=settings.database_path)
    ok, err = orch.health_probe("balanced_classifier")
    if not ok:
        typer.echo(f"ERROR: balanced_classifier health check failed: {err}")
        raise typer.Exit(code=1)

    adjudicator = LLMAdjudicator(orch, db_path=settings.database_path, role="balanced_classifier")

    # 3. Adjudicate each candidate
    results: list[dict] = []
    for idx, c in enumerate(candidates, 1):
        subj_id = c["subject_id"]
        typer.echo(f"\n[{idx}/{len(candidates)}] subject_id={subj_id}")

        # Get document path and text
        conn = connect(settings.database_path)
        try:
            doc = conn.execute(
                "SELECT local_path FROM historical_documents WHERE id = ?",
                (subj_id,),
            ).fetchone()
        finally:
            conn.close()

        if not doc:
            typer.echo("  SKIP: document not found in historical_documents")
            continue

        local_path = doc[0]
        slices = slice_pdf_text(Path(local_path), max_chars=2500)
        text = slices.full_text or ""

        if not text:
            typer.echo("  SKIP: no text extractable")
            continue

        # Parse prior results
        rule_result = ClassificationResult(
            label=c["rule_label"],
            confidence=float(c["rule_confidence"]),
            classifier="rule_document_type_v1",
        )
        emb_result = ClassificationResult(
            label=c["emb_label"],
            confidence=float(c["emb_confidence"]),
            classifier="embedding_knn_v1",
        )

        # Run adjudication
        llm_result = adjudicator.adjudicate(
            text, rule_result=rule_result, embedding_result=emb_result
        )

        typer.echo(
            f"  LLM says: {llm_result.label} (conf={llm_result.confidence:.3f}, "
            f"classifier={llm_result.classifier})"
        )

        # Persist
        conn = connect(settings.database_path)
        try:
            row_id = record_classification(
                conn,
                subject_kind="historical_document",
                subject_id=str(subj_id),
                stage="document_type",
                result=llm_result,
            )
            conn.commit()
        except Exception as exc:
            typer.echo(f"  WARN: persist failed: {exc}")
        finally:
            conn.close()

        results.append({
            "subject_id": subj_id,
            "rule_label": c["rule_label"],
            "rule_confidence": round(float(c["rule_confidence"]), 3),
            "emb_label": c["emb_label"],
            "emb_confidence": round(float(c["emb_confidence"]), 3),
            "llm_label": llm_result.label,
            "llm_confidence": llm_result.confidence,
            "llm_classifier": llm_result.classifier,
            "agrees_with": (
                "rule" if llm_result.label == c["rule_label"]
                else "embedding" if llm_result.label == c["emb_label"]
                else "neither"
            ),
        })

    # 4. Summary
    if as_json:
        typer.echo(json.dumps(results, indent=2, default=str))
        return

    if not results:
        typer.echo("\nNo results to report.")
        return

    typer.echo("\n--- Adjudication Summary ---")
    typer.echo(f"{'subj_id':>8s} {'rule':<20s} {'r_c':>5s} {'emb':<20s} {'e_c':>5s} {'llm':<20s} {'l_c':>5s} {'agrees'}")
    typer.echo("-" * 100)
    for r in results:
        typer.echo(
            f"{r['subject_id']:>8s} "
            f"{r['rule_label']:<20s} {r['rule_confidence']:>5.3f} "
            f"{r['emb_label']:<20s} {r['emb_confidence']:>5.3f} "
            f"{r['llm_label']:<20s} {r['llm_confidence']:>5.3f} "
            f"{r['agrees_with']}"
        )

    rule_agree = sum(1 for r in results if r["agrees_with"] == "rule")
    emb_agree = sum(1 for r in results if r["agrees_with"] == "embedding")
    neither = sum(1 for r in results if r["agrees_with"] == "neither")
    typer.echo(
        f"\nAgrees with rule: {rule_agree}, embedding: {emb_agree}, neither: {neither}"
    )


@doc_intel_app.command("fingerprint-corpus")
def fingerprint_corpus_nc(
    limit: int = typer.Option(0, "--limit", help="Max PDFs to process (0 = all)."),
    refresh: bool = typer.Option(False, "--refresh", help="Re-fingerprint PDFs even if a row already exists at the current fingerprinter version."),
    progress: bool = typer.Option(True, "--progress/--no-progress", help="Emit per-PDF progress to stderr."),
) -> None:
    """Fingerprint every PDF referenced by ``historical_documents`` and ``ncuc_discovery_records``.

    Populates ``document_fingerprints_v2`` so cluster reports have data.
    Idempotent at the current fingerprinter version — existing rows are
    skipped unless ``--refresh`` is passed.

    This is the bootstrap pass; new PDFs encountered during ingestion will
    be fingerprinted by the importer itself once that wiring lands.
    """
    from duke_rates.classification.fingerprint import (
        FINGERPRINTER_VERSION, fingerprint_pdf, save_fingerprint,
    )
    from duke_rates.db.sqlite import connect

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT local_path FROM (
                SELECT local_path FROM historical_documents WHERE local_path IS NOT NULL
                UNION
                SELECT local_path FROM ncuc_discovery_records WHERE local_path IS NOT NULL
            )
            ORDER BY local_path
            """
        ).fetchall()
        paths = [r["local_path"] for r in rows]
        if limit > 0:
            paths = paths[:limit]

        already_fingerprinted: set[str] = set()
        if not refresh:
            existing = conn.execute(
                "SELECT source_pdf FROM document_fingerprints_v2 WHERE fingerprinter_version = ?",
                (FINGERPRINTER_VERSION,),
            ).fetchall()
            already_fingerprinted = {r["source_pdf"] for r in existing}

        processed = 0
        skipped = 0
        failed = 0
        for i, path in enumerate(paths, 1):
            if path in already_fingerprinted:
                skipped += 1
                continue
            fp = fingerprint_pdf(path)
            if fp is None:
                failed += 1
                continue
            try:
                save_fingerprint(conn, fp)
                conn.commit()
                processed += 1
            except Exception:
                failed += 1
            if progress and i % 50 == 0:
                typer.echo(
                    f"[{i}/{len(paths)}] processed={processed} skipped={skipped} failed={failed}",
                    err=True,
                )
    finally:
        conn.close()

    typer.echo(
        f"doc-intel fingerprint-corpus done: total={len(paths)} "
        f"processed={processed} skipped={skipped} failed={failed}"
    )


@doc_intel_app.command("embed-corpus")
def embed_corpus_nc(
    limit: int = typer.Option(0, "--limit", help="Max PDFs to process (0 = all)."),
    refresh: bool = typer.Option(False, "--refresh", help="Re-embed even if a row already exists at the current embedding version."),
    embedding_kind: str = typer.Option("full_text", "--kind", help="Which text slice to embed: full_text, first_3_pages, title_block, rate_table_text, order_conclusion_section."),
    max_chars: int = typer.Option(2000, "--max-chars", help="Truncate text to this many characters before embedding (stays within model context windows)."),
    progress: bool = typer.Option(True, "--progress/--no-progress", help="Emit progress to stderr."),
) -> None:
    """Generate embeddings for every PDF referenced by ``historical_documents``.

    Populates ``document_embeddings`` so the embedding classifier has a
    reference population. Idempotent — existing (source_pdf, file_hash,
    embedding_kind, embedding_model, embedding_version) rows are skipped
    unless ``--refresh`` is passed.

    Runs against both ``embedding_primary`` and ``embedding_secondary``
    model roles, producing one row per model per slice.
    """
    import struct

    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.ollama_orchestrator import (
        OllamaOrchestrator,
    )
    from duke_rates.document_intelligence.text_slicer import slice_pdf_text

    EMBEDDING_VERSION = "v1"
    EMBEDDING_ROLES = ["embedding_primary", "embedding_secondary"]

    settings, _ = _bootstrap()

    conn = connect(settings.database_path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT hd.local_path, hd.content_hash
            FROM historical_documents hd
            WHERE hd.local_path IS NOT NULL AND hd.local_path != ''
            ORDER BY hd.local_path
            """
        ).fetchall()
    finally:
        conn.close()

    docs = [(r["local_path"], r["content_hash"]) for r in rows]
    if limit > 0:
        docs = docs[:limit]

    orchestrator = OllamaOrchestrator()

    # Resolve model names for the embedding roles
    role_models: list[tuple[str, str]] = []
    for role in EMBEDDING_ROLES:
        try:
            ok, msg = orchestrator.health_probe(role)
            if not ok:
                typer.echo(f"Warning: {role} unavailable ({msg}) — skipping", err=True)
                continue
            model = orchestrator._roles[role].primary
            role_models.append((role, model))
        except Exception:
            typer.echo(f"Warning: {role} not configured — skipping", err=True)

    if not role_models:
        typer.echo("No embedding models available. Check ollama_models.yaml.", err=True)
        return

    typer.echo(
        f"Embedding {len(docs)} PDFs × {len(role_models)} model(s) "
        f"(kind={embedding_kind})..."
    )

    # Build idempotency set of (source_pdf, file_hash, embedding_model) already present
    already_embedded: set[tuple[str, str, str]] = set()
    if not refresh:
        conn = connect(settings.database_path)
        try:
            for role, model in role_models:
                existing = conn.execute(
                    """
                    SELECT source_pdf, file_hash, embedding_model
                    FROM document_embeddings
                    WHERE embedding_kind = ?
                      AND embedding_model = ?
                      AND embedding_version = ?
                    """,
                    (embedding_kind, model, EMBEDDING_VERSION),
                ).fetchall()
                for r in existing:
                    already_embedded.add(
                        (r["source_pdf"], r["file_hash"], r["embedding_model"])
                    )
        finally:
            conn.close()

    processed = 0
    skipped = 0
    failed = 0
    for i, (local_path, file_hash) in enumerate(docs, 1):
        path = Path(local_path)
        if not path.exists():
            failed += 1
            if progress:
                typer.echo(
                    f"  [{i}/{len(docs)}] missing: {local_path}", err=True
                )
            continue

        # Extract text slice
        try:
            slices = slice_pdf_text(path, max_chars=max_chars)
        except Exception:
            failed += 1
            continue

        text = ""
        if embedding_kind == "full_text":
            text = slices.full_text
        elif embedding_kind == "first_3_pages":
            text = slices.first_3_pages
        elif embedding_kind == "title_block":
            text = slices.title_block
        elif embedding_kind == "rate_table_text":
            text = slices.rate_table_text
        elif embedding_kind == "order_conclusion_section":
            text = slices.order_conclusion_section

        if not text or not text.strip():
            skipped += 1
            continue

        any_embedded = False
        for role, model in role_models:
            if (local_path, file_hash, model) in already_embedded:
                continue

            try:
                vector = orchestrator.embed(role, text)
            except Exception:
                failed += 1
                if progress:
                    typer.echo(
                        f"  [{i}/{len(docs)}] embed failed: {local_path} "
                        f"({role}/{model})",
                        err=True,
                    )
                continue

            try:
                blob = struct.pack("f" * len(vector), *vector)
            except Exception:
                failed += 1
                continue

            conn = connect(settings.database_path)
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO document_embeddings
                        (source_pdf, file_hash, embedding_kind, embedding_model,
                         embedding_version, vector)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (local_path, file_hash, embedding_kind, model,
                     EMBEDDING_VERSION, blob),
                )
                conn.commit()
                any_embedded = True
            except Exception:
                failed += 1
            finally:
                conn.close()

        if any_embedded:
            processed += 1
        else:
            skipped += 1

        if progress and i % 10 == 0:
            typer.echo(
                f"[{i}/{len(docs)}] processed={processed} skipped={skipped} failed={failed}",
                err=True,
            )

    typer.echo(
        f"doc-intel embed-corpus done: total={len(docs)} "
        f"processed={processed} skipped={skipped} failed={failed}"
    )


@doc_intel_app.command("backfill-embedding-classifications")
def backfill_embedding_classifications_nc(
    limit: int = typer.Option(0, "--limit", help="Only process N documents (0 = all)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Classify but do not persist."),
    progress: bool = typer.Option(True, "--progress/--no-progress", help="Emit progress to stderr."),
    label_source: str = typer.Option(
        "rule_v1",
        "--label-source",
        help=(
            "Source of neighbor doc_type labels for KNN voting: "
            "'rule_v1' (legacy), 'section_gold' (section_type_gold only), "
            "or 'section_gold_or_rule' (preferred — falls back to rule_v1)."
        ),
    ),
    rerun: bool = typer.Option(
        False,
        "--rerun",
        help=(
            "Re-classify documents that already have an active embedding_knn_v1 "
            "row. The new (v2) row is written and the old row is superseded. "
            "Use this when switching --label-source on already-classified docs."
        ),
    ),
) -> None:
    """Backfill embedding-based document_type classifications for existing documents.

    Runs the embedding KNN classifier against each historical_document that
    has embeddings in the reference table, and persists a second
    ``document_type`` row with ``classifier='embedding_knn_v1'``.

    Requires document_embeddings to be populated first (via doc-intel embed-corpus).
    """
    from pathlib import Path

    from duke_rates.classification.persistence import record_classification
    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.embedding_classifier import (
        EmbeddingKNNClassifier,
    )
    from duke_rates.document_intelligence.ollama_orchestrator import (
        OllamaOrchestrator,
    )

    settings, _ = _bootstrap()

    conn = connect(settings.database_path)
    try:
        if rerun:
            # Include all docs with local_path — we'll re-classify and supersede.
            rows = conn.execute(
                """
                SELECT hd.id, hd.local_path, hd.family_key
                FROM historical_documents hd
                WHERE hd.local_path IS NOT NULL AND hd.local_path != ''
                ORDER BY hd.id
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT hd.id, hd.local_path, hd.family_key
                FROM historical_documents hd
                WHERE hd.local_path IS NOT NULL AND hd.local_path != ''
                  AND hd.id NOT IN (
                      SELECT DISTINCT CAST(subject_id AS INTEGER)
                      FROM document_classifications
                      WHERE subject_kind = 'historical_document'
                        AND stage = 'document_type'
                        AND classifier = 'embedding_knn_v1'
                        AND superseded_by IS NULL
                  )
                ORDER BY hd.id
                """
            ).fetchall()
    finally:
        conn.close()

    docs = [dict(r) for r in rows]
    if limit > 0:
        docs = docs[:limit]

    if not docs:
        typer.echo("All documents already have embedding classifications.")
        return

    # Check that reference embeddings exist
    conn = connect(settings.database_path)
    try:
        emb_count = conn.execute(
            "SELECT COUNT(*) FROM document_embeddings"
        ).fetchone()[0]
    finally:
        conn.close()

    if emb_count == 0:
        typer.echo(
            "No embeddings found in document_embeddings. "
            "Run doc-intel embed-corpus first.",
            err=True,
        )
        return

    typer.echo(
        f"Backfilling embedding classifications for {len(docs)} documents "
        f"(reference set: {emb_count} embeddings)..."
    )

    if dry_run:
        typer.echo("[DRY RUN — no rows will be written]")

    orch = OllamaOrchestrator()
    clf = EmbeddingKNNClassifier(
        db_path=settings.database_path,
        orchestrator=orch,
        model_role="embedding_primary",
        k=11,
        min_neighbors=3,
        embedding_kind="full_text",
        label_source=label_source,
    )

    ok = skip = fail = 0
    for i, doc in enumerate(docs):
        doc_id = doc.get("id")
        local_path = doc.get("local_path", "")
        if not local_path or not Path(local_path).exists():
            skip += 1
            continue

        try:
            result = clf.classify(local_path)
        except Exception:
            fail += 1
            continue

        if result.label == "UNKNOWN" and result.confidence == 0.0:
            skip += 1
            continue

        if dry_run:
            ok += 1
        else:
            cls_conn = connect(settings.database_path)
            try:
                new_id = record_classification(
                    cls_conn,
                    subject_kind="historical_document",
                    subject_id=str(doc_id),
                    stage="document_type",
                    result=result,
                )
                # When rerunning, supersede prior active rows of a different
                # version. Same-version rows were UPDATE-in-place by
                # record_classification so they aren't duplicates.
                if rerun:
                    cls_conn.execute(
                        """
                        UPDATE document_classifications
                        SET superseded_by = ?
                        WHERE subject_kind = 'historical_document'
                          AND subject_id = ?
                          AND stage = 'document_type'
                          AND classifier = ?
                          AND classifier_version != ?
                          AND superseded_by IS NULL
                          AND id != ?
                        """,
                        (
                            new_id,
                            str(doc_id),
                            result.classifier,
                            result.classifier_version,
                            new_id,
                        ),
                    )
                cls_conn.commit()
                ok += 1
            except Exception:
                fail += 1
            finally:
                cls_conn.close()

        if progress and (i + 1) % 25 == 0:
            typer.echo(
                f"  {i + 1}/{len(docs)} ok={ok} skip={skip} fail={fail}",
                err=True,
            )

    typer.echo(f"\nDone: ok={ok} skip={skip} fail={fail}")


@doc_intel_app.command("run-overnight")
def run_overnight_doc_intelligence_nc(
    max_documents: int = typer.Option(0, "--max-documents", help="Max documents to process (0 = unlimited)."),
    max_runtime_minutes: int = typer.Option(0, "--max-runtime-minutes", help="Hard wall-clock cap in minutes (0 = unlimited)."),
    max_consecutive_failures: int = typer.Option(5, "--max-consecutive-failures", help="Abort after N consecutive model call failures."),
    stages: str = typer.Option("embed,llm_adjudicate", "--stages", help="Comma-separated stages: embed, llm_adjudicate."),
    since: str = typer.Option("", "--since", help="ISO8601 datetime — only process documents added/modified after this."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Enumerate work set and exit without model calls or DB writes."),
    resume: bool = typer.Option(False, "--resume", help="Skip subjects already covered at current prompt_version + model."),
    progress_interval: int = typer.Option(10, "--progress-interval", help="Emit progress every N documents."),
    health_probe_interval: int = typer.Option(50, "--health-probe-interval", help="Re-probe Ollama health every N documents."),
) -> None:
    """Run embedding generation + LLM adjudication as a resumable overnight batch.

    Processes the corpus in two sequential stages per document:
      1. **embed** — generate embeddings with ``embedding_primary`` role
      2. **llm_adjudicate** — run LLM on rule/embedding disagreements

    Safety guarantees:
      - No destructive overwrites — only INSERTs new rows
      - Bounded by wall-clock cap even with unlimited --max-documents
      - Resumable — --resume skips completed (subject, stage, model, prompt_version) tuples
      - Stops cleanly on: max docs, max runtime, consecutive failures, health probe degradation, SIGINT/SIGTERM

    End-of-run JSON report written to docs/reports/overnight_doc_intelligence/<timestamp>.json
    """
    import json as _json_mod
    import signal as _signal
    import struct
    import sqlite3
    import time
    from datetime import datetime as _datetime, timezone as _timezone
    from pathlib import Path as _Path

    from duke_rates.classification.persistence import record_classification
    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator
    from duke_rates.document_intelligence.text_slicer import slice_pdf_text

    EMBEDDING_VERSION = "v1"
    EMBEDDING_ROLE = "embedding_primary"
    LLM_ROLE = "balanced_classifier"
    LLM_VERSION = "v1"

    settings, _ = _bootstrap()

    # ------------------------------------------------------------------
    # Parse stages
    # ------------------------------------------------------------------
    stage_list = [s.strip() for s in stages.split(",") if s.strip()]
    valid_stages = {"embed", "llm_adjudicate"}
    for s in stage_list:
        if s not in valid_stages:
            typer.echo(f"Unknown stage {s!r}. Valid: {', '.join(sorted(valid_stages))}", err=True)
            raise typer.Exit(code=1)

    typer.echo(f"Stages: {stage_list}")

    # ------------------------------------------------------------------
    # Health probes
    # ------------------------------------------------------------------
    orch = OllamaOrchestrator(db_path=settings.database_path)
    needed_roles: set[str] = set()
    if "embed" in stage_list:
        needed_roles.add(EMBEDDING_ROLE)
    if "llm_adjudicate" in stage_list:
        needed_roles.add(LLM_ROLE)

    role_models: dict[str, str] = {}
    for role in needed_roles:
        ok, err = orch.health_probe(role)
        if not ok:
            typer.echo(f"ERROR: {role} health check failed: {err}", err=True)
            raise typer.Exit(code=1)
        role_models[role] = orch._roles[role].primary
        typer.echo(f"  {role} -> {role_models[role]} (OK)")

    # ------------------------------------------------------------------
    # Build work set
    # ------------------------------------------------------------------
    conn = connect(settings.database_path)
    try:
        params: list = []
        extra_where = ""
        if since:
            extra_where = " AND hd.retrieved_at >= ?"
            params.append(since)

        rows = conn.execute(
            f"""
            SELECT DISTINCT hd.id, hd.local_path, hd.content_hash, hd.family_key, hd.retrieved_at
            FROM historical_documents hd
            WHERE hd.local_path IS NOT NULL AND hd.local_path != ''
              AND hd.local_path != 'embedded'
              {extra_where}
            ORDER BY hd.id
            """,
            tuple(params),
        ).fetchall()
    finally:
        conn.close()

    docs = [dict(r) for r in rows]
    typer.echo(f"Corpus: {len(docs)} documents")

    # ------------------------------------------------------------------
    # Resume filter: check ollama_model_runs for completed subjects
    # ------------------------------------------------------------------
    if resume:
        conn = connect(settings.database_path)
        try:
            completed: set[tuple[int, str, str]] = set()
            for doc in docs:
                doc_id = doc["id"]
                # Check each stage
                for stage in stage_list:
                    role = EMBEDDING_ROLE if stage == "embed" else LLM_ROLE
                    model = role_models[role]
                    existing = conn.execute(
                        """
                        SELECT id FROM ollama_model_runs
                        WHERE subject_kind = 'historical_document'
                          AND CAST(subject_id AS INTEGER) = ?
                          AND stage = ?
                          AND role = ?
                          AND model = ?
                          AND prompt_version = ?
                          AND status = 'ok'
                        LIMIT 1
                        """,
                        (doc_id, stage, role, model, LLM_VERSION if stage == "llm_adjudicate" else EMBEDDING_VERSION),
                    ).fetchone()
                    if existing:
                        completed.add((doc_id, stage, role))
        finally:
            conn.close()

        # Filter
        original_count = len(docs)
        filtered: list[dict] = []
        for doc in docs:
            doc_id = doc["id"]
            needed = 0
            done = 0
            for stage in stage_list:
                role = EMBEDDING_ROLE if stage == "embed" else LLM_ROLE
                needed += 1
                if (doc_id, stage, role) in completed:
                    done += 1
            if done < needed:
                filtered.append(doc)
        docs = filtered
        typer.echo(f"Resume: {len(docs)} remaining (skipped {original_count - len(docs)} already completed)")

    if not docs:
        typer.echo("No documents to process.")
        return

    # ------------------------------------------------------------------
    # Dry run
    # ------------------------------------------------------------------
    if dry_run:
        embed_count = sum(1 for _ in docs) * (1 if "embed" in stage_list else 0)
        llm_count = 0
        if "llm_adjudicate" in stage_list:
            conn = connect(settings.database_path)
            try:
                llm_docs = set()
                for doc in docs:
                    # Check if rule/embedding disagree for this doc
                    pair = conn.execute(
                        """
                        SELECT 1 FROM document_classifications r
                        JOIN document_classifications e
                          ON e.subject_kind = r.subject_kind
                         AND e.subject_id = r.subject_id
                         AND e.stage = r.stage
                         AND e.classifier = 'embedding_knn_v1'
                         AND e.superseded_by IS NULL
                        WHERE r.subject_kind = 'historical_document'
                          AND r.subject_id = CAST(? AS TEXT)
                          AND r.stage = 'document_type'
                          AND r.classifier = 'rule_document_type_v1'
                          AND r.superseded_by IS NULL
                          AND (
                              r.label != e.label
                              OR r.label = 'UNKNOWN' OR e.label = 'UNKNOWN'
                              OR MAX(r.confidence, e.confidence) < 0.5
                          )
                        """,
                        (doc["id"],),
                    ).fetchone()
                    if pair:
                        llm_docs.add(doc["id"])
                llm_count = len(llm_docs)
            finally:
                conn.close()

        typer.echo("\n--- Dry Run Work Set ---")
        typer.echo(f"  embed calls:          {embed_count}")
        typer.echo(f"  llm_adjudicate calls: {llm_count}")
        typer.echo(f"  total documents:      {len(docs)}")

        # Estimate runtime
        est_embed_s = embed_count * 2.0  # ~2s per embedding
        est_llm_s = llm_count * 8.0       # ~8s per LLM call
        est_total_s = est_embed_s + est_llm_s
        if est_total_s < 60:
            typer.echo(f"  est. runtime:         {est_total_s:.0f}s")
        elif est_total_s < 3600:
            typer.echo(f"  est. runtime:         {est_total_s / 60:.1f}m")
        else:
            typer.echo(f"  est. runtime:         {est_total_s / 3600:.1f}h")
        return

    # ------------------------------------------------------------------
    # Signal handling for clean exit
    # ------------------------------------------------------------------
    _abort_flag = {"value": False}

    def _handle_signal(signum, frame):
        typer.echo("\nInterrupted — finishing current document and exiting...", err=True)
        _abort_flag["value"] = True

    _signal.signal(_signal.SIGINT, _handle_signal)
    _signal.signal(_signal.SIGTERM, _handle_signal)

    # ------------------------------------------------------------------
    # Initialize stage processors
    # ------------------------------------------------------------------
    embed_clf = None
    if "embed" in stage_list:
        from duke_rates.document_intelligence.embedding_classifier import EmbeddingKNNClassifier

    llm_adj = None
    if "llm_adjudicate" in stage_list:
        from duke_rates.document_intelligence.llm_classifier import LLMAdjudicator
        llm_adj = LLMAdjudicator(orch, db_path=settings.database_path, role=LLM_ROLE)

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    start_time = time.monotonic()
    wall_deadline = (
        start_time + (max_runtime_minutes * 60)
        if max_runtime_minutes > 0
        else float("inf")
    )

    stats: dict[str, dict] = {
        "embed": {"ok": 0, "skip": 0, "fail": 0, "no_text": 0},
        "llm_adjudicate": {"ok": 0, "skip": 0, "fail": 0, "not_needed": 0},
    }
    consecutive_failures = 0
    last_health_probe = 0
    stop_reason = "completed"

    for doc_idx, doc in enumerate(docs):
        doc_id = doc["id"]
        local_path = doc.get("local_path", "")

        # --- Stop checks before processing ---
        if _abort_flag["value"]:
            stop_reason = "interrupted"
            break

        if max_documents > 0 and doc_idx >= max_documents:
            stop_reason = "max_documents"
            break

        if time.monotonic() >= wall_deadline:
            stop_reason = "max_runtime"
            break

        if consecutive_failures >= max_consecutive_failures:
            stop_reason = "max_consecutive_failures"
            break

        # Periodic health re-probe
        if doc_idx - last_health_probe >= health_probe_interval:
            last_health_probe = doc_idx
            for role in needed_roles:
                ok_hp, err_hp = orch.health_probe(role)
                if not ok_hp:
                    stop_reason = f"health_probe_failed:{role}"
                    typer.echo(
                        f"\nHealth probe failed for {role}: {err_hp} — stopping.",
                        err=True,
                    )
                    break
            if stop_reason != "completed":
                break

        path = _Path(local_path)
        if not path.exists():
            stats["embed"]["skip"] += 1
            stats["llm_adjudicate"]["skip"] += 1
            continue

        # --- Stage: embed ---
        if "embed" in stage_list:
            # Idempotency check
            conn = connect(settings.database_path)
            try:
                existing_emb = conn.execute(
                    """
                    SELECT id FROM document_embeddings
                    WHERE source_pdf = ?
                      AND file_hash = ?
                      AND embedding_kind = 'full_text'
                      AND embedding_model = ?
                      AND embedding_version = ?
                    """,
                    (local_path, doc.get("content_hash", ""),
                     role_models[EMBEDDING_ROLE], EMBEDDING_VERSION),
                ).fetchone()
            finally:
                conn.close()

            if existing_emb:
                stats["embed"]["skip"] += 1
            else:
                try:
                    slices = slice_pdf_text(path, max_chars=2000)
                    text = slices.full_text or ""
                except Exception:
                    stats["embed"]["fail"] += 1
                    consecutive_failures += 1
                    text = ""

                if not text or not text.strip():
                    stats["embed"]["no_text"] += 1
                else:
                    try:
                        vector = orch.embed(EMBEDDING_ROLE, text)
                        blob = struct.pack("f" * len(vector), *vector)

                        conn = connect(settings.database_path)
                        try:
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO document_embeddings
                                    (source_pdf, file_hash, embedding_kind, embedding_model,
                                     embedding_version, vector)
                                VALUES (?, ?, ?, ?, ?, ?)
                                """,
                                (local_path, doc.get("content_hash", ""), "full_text",
                                 role_models[EMBEDDING_ROLE], EMBEDDING_VERSION, blob),
                            )
                            conn.commit()
                        finally:
                            conn.close()
                        stats["embed"]["ok"] += 1
                        consecutive_failures = 0
                    except Exception:
                        stats["embed"]["fail"] += 1
                        consecutive_failures += 1

        # --- Stage: llm_adjudicate ---
        if "llm_adjudicate" in stage_list:
            # Check if this doc has both rule and embedding classifications
            conn = connect(settings.database_path)
            try:
                # Skip if already adjudicated
                existing_llm = conn.execute(
                    """
                    SELECT id FROM document_classifications
                    WHERE subject_kind = 'historical_document'
                      AND subject_id = CAST(? AS TEXT)
                      AND stage = 'document_type'
                      AND classifier LIKE 'llm_%'
                      AND superseded_by IS NULL
                    """,
                    (doc_id,),
                ).fetchone()

                if existing_llm:
                    stats["llm_adjudicate"]["skip"] += 1
                    conn.close()
                    continue

                pair = conn.execute(
                    """
                    SELECT r.label AS rule_label, r.confidence AS rule_confidence,
                           e.label AS emb_label, e.confidence AS emb_confidence
                    FROM document_classifications r
                    JOIN document_classifications e
                      ON e.subject_kind = r.subject_kind
                     AND e.subject_id = r.subject_id
                     AND e.stage = r.stage
                     AND e.classifier = 'embedding_knn_v1'
                     AND e.superseded_by IS NULL
                    WHERE r.subject_kind = 'historical_document'
                      AND r.subject_id = CAST(? AS TEXT)
                      AND r.stage = 'document_type'
                      AND r.classifier = 'rule_document_type_v1'
                      AND r.superseded_by IS NULL
                    """,
                    (doc_id,),
                ).fetchone()
            finally:
                conn.close()

            if not pair:
                stats["llm_adjudicate"]["not_needed"] += 1
                continue

            rule_label = pair["rule_label"] or "UNKNOWN"
            emb_label = pair["emb_label"] or "UNKNOWN"
            rule_conf = float(pair["rule_confidence"] or 0)
            emb_conf = float(pair["emb_confidence"] or 0)

            need_adjudication = (
                rule_label != emb_label
                or rule_label == "UNKNOWN" or emb_label == "UNKNOWN"
                or max(rule_conf, emb_conf) < 0.5
            )

            if not need_adjudication:
                stats["llm_adjudicate"]["not_needed"] += 1
                continue

            # Extract text
            slices = slice_pdf_text(path, max_chars=2500)
            text = slices.full_text or ""
            if not text:
                stats["llm_adjudicate"]["no_text"] = stats["llm_adjudicate"].get("no_text", 0) + 1
                continue

            try:
                from duke_rates.classification.result import ClassificationResult

                rule_result = ClassificationResult(
                    label=rule_label, confidence=rule_conf, classifier="rule_document_type_v1",
                )
                emb_result = ClassificationResult(
                    label=emb_label, confidence=emb_conf, classifier="embedding_knn_v1",
                )
                llm_result = llm_adj.adjudicate(
                    text, rule_result=rule_result, embedding_result=emb_result,
                )

                if llm_result.label != "UNKNOWN":
                    conn = connect(settings.database_path)
                    try:
                        record_classification(
                            conn,
                            subject_kind="historical_document",
                            subject_id=str(doc_id),
                            stage="document_type",
                            result=llm_result,
                        )
                        conn.commit()
                    finally:
                        conn.close()

                stats["llm_adjudicate"]["ok"] += 1
                consecutive_failures = 0
            except Exception:
                stats["llm_adjudicate"]["fail"] += 1
                consecutive_failures += 1

        # --- Progress ---
        if progress_interval > 0 and (doc_idx + 1) % progress_interval == 0:
            elapsed = time.monotonic() - start_time
            rate = (doc_idx + 1) / elapsed if elapsed > 0 else 0
            typer.echo(
                f"  [{doc_idx + 1}/{len(docs)}] "
                f"embed ok={stats['embed']['ok']} fail={stats['embed']['fail']} | "
                f"llm ok={stats['llm_adjudicate']['ok']} fail={stats['llm_adjudicate']['fail']} | "
                f"{rate:.1f} docs/s",
                err=True,
            )

    # ------------------------------------------------------------------
    # End-of-run report
    # ------------------------------------------------------------------
    elapsed_total = time.monotonic() - start_time
    report_dir = _Path("docs/reports/overnight_doc_intelligence")
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = _datetime.now(_timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = report_dir / f"{timestamp}.json"

    report = {
        "timestamp": timestamp,
        "stop_reason": stop_reason,
        "config": {
            "stages": stage_list,
            "max_documents": max_documents if max_documents > 0 else None,
            "max_runtime_minutes": max_runtime_minutes if max_runtime_minutes > 0 else None,
            "max_consecutive_failures": max_consecutive_failures,
            "resume": resume,
            "since": since or None,
        },
        "runtime": {
            "total_seconds": round(elapsed_total, 1),
            "documents_processed": len(docs),
            "docs_per_second": round(len(docs) / elapsed_total, 2) if elapsed_total > 0 else 0,
        },
        "stats": {
            stage: {
                k: v for k, v in s.items()
            }
            for stage, s in stats.items()
        },
        "roles_used": {role: model for role, model in role_models.items()},
    }

    with open(report_path, "w", encoding="utf-8") as fh:
        _json_mod.dump(report, fh, indent=2, default=str)

    # ------------------------------------------------------------------
    # Summary to stderr
    # ------------------------------------------------------------------
    typer.echo(f"\n--- Overnight Run Complete ---")
    typer.echo(f"  stop reason:   {stop_reason}")
    typer.echo(f"  elapsed:       {elapsed_total:.1f}s ({elapsed_total / 60:.1f}m)")
    typer.echo(f"  documents:     {len(docs)}")
    for stage, s in stats.items():
        parts = ", ".join(f"{k}={v}" for k, v in s.items() if v > 0)
        typer.echo(f"  {stage}: {parts}")
    typer.echo(f"  report:        {report_path}")


@doc_intel_app.command("backfill-classifications")
def backfill_classifications_nc(
    limit: int = typer.Option(0, "--limit", help="Max historical docs to backfill (0 = all)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be recorded without writing."),
) -> None:
    """Backfill document_classifications rows for existing historical documents.

    The classification persistence was wired into the importer after most
    historical docs had already been created.  This command fills in the
    missing ``family_mapping`` classification rows using the evidence that
    was already stored in ``historical_documents.evidence_json`` at import
    time.

    Idempotent — running it again skips docs that already have a
    ``family_mapping`` classification at the current classifier version.
    """
    import json as _json

    settings, _ = _bootstrap()
    conn = None
    conn_classify = None
    try:
        from duke_rates.db.sqlite import connect
        from duke_rates.classification.result import ClassificationResult
        from duke_rates.classification.persistence import record_classification

        conn = connect(settings.database_path)
        conn.row_factory = __import__("sqlite3").Row

        rows = conn.execute(
            """
            SELECT hd.id, hd.family_key, hd.evidence_json, hd.title
            FROM historical_documents hd
            WHERE hd.family_key IS NOT NULL
              AND hd.family_key != ''
              AND hd.evidence_json IS NOT NULL
              AND hd.evidence_json != ''
              AND hd.evidence_json != '{}'
              AND NOT EXISTS (
                SELECT 1 FROM document_classifications dc
                WHERE dc.subject_kind = 'historical_document'
                  AND dc.subject_id = CAST(hd.id AS TEXT)
                  AND dc.stage = 'family_mapping'
                  AND dc.classifier = 'family_matcher_v1'
              )
            ORDER BY hd.id
            """
        ).fetchall()
        total = len(rows)
        if limit > 0:
            rows = rows[:limit]

        typer.echo(
            f"Backfill candidates: {total} total, processing {len(rows)}"
            + (" (dry run)" if dry_run else "")
        )

        recorded = 0
        skipped_empty_evidence = 0
        for row in rows:
            try:
                evidence_raw = _json.loads(row["evidence_json"])
            except (_json.JSONDecodeError, TypeError):
                evidence_raw = {}

            # evidence_json can contain non-numeric fields
            # (is_redline bool, redline_notes str, etc.) — extract only
            # numeric score contributions for the ClassificationResult.
            numeric_evidence: dict[str, float] = {}
            for k, v in evidence_raw.items():
                try:
                    numeric_evidence[k] = float(v)
                except (ValueError, TypeError):
                    pass
            if not numeric_evidence:
                skipped_empty_evidence += 1
                continue

            total_score = sum(numeric_evidence.values())
            result = ClassificationResult.from_score_breakdown(
                label=row["family_key"],
                score=total_score,
                score_breakdown=numeric_evidence,
                all_scores={row["family_key"]: total_score},
                classifier="family_matcher_v1",
                classifier_version="backfill_v1",
            )

            if dry_run:
                typer.echo(
                    f"  [dry-run] hd={row['id']} label={result.label} "
                    f"confidence={result.confidence:.2f} "
                    f"evidence_keys={list(evidence_raw.keys())[:3]}"
                )
            else:
                conn_classify = conn_classify or connect(settings.database_path)
                record_classification(
                    conn_classify,
                    subject_kind="historical_document",
                    subject_id=str(row["id"]),
                    stage="family_mapping",
                    result=result,
                )
            recorded += 1

        if not dry_run and conn_classify:
            conn_classify.commit()

        typer.echo(
            f"doc-intel backfill-classifications done: recorded={recorded} "
            f"skipped_empty_evidence={skipped_empty_evidence}"
        )
    finally:
        if conn_classify:
            conn_classify.close()
        if conn:
            conn.close()


@doc_intel_app.command("process-docling-batch")
def process_docling_batch(
    accelerator: str = typer.Option("cpu", help="Accelerator: cpu or cuda."),
    limit: int = typer.Option(0, help="Max documents to process (0 = all)."),
    classification: str = typer.Option(
        "",
        help="Filter by filing_classification (e.g. tariff_sheets, order, testimony). Empty = all.",
    ),
    scanned: bool = typer.Option(False, "--scanned", help="Enable Tesseract OCR for all documents."),
    force: bool = typer.Option(False, "--force", help="Re-process documents already in DB."),
    dry_run: bool = typer.Option(False, "--dry-run", help="List documents that would be processed without running Docling."),
    source: str = typer.Option(
        "discovery",
        help="Source table: 'discovery' (ncuc_discovery_records) or 'historical' (historical_documents NC).",
    ),
    ocr_remediation: bool = typer.Option(
        False, "--ocr-remediation",
        help="Target only historical_documents flagged as OCR remediation candidates (run_docling_or_paddle_structure lane). Implies --source historical --scanned.",
    ),
) -> None:
    """Process a batch of NCUC documents through Docling in a single long-running process.

    Runs unprocessed local PDFs through Docling and stores results in the docling_artifacts
    table.  Keeping everything in one process avoids the CUDA DLL cold-start penalty
    (3-5 minutes) that would occur if each document were run as a separate invocation.

    The GPU/CUDA DLLs are loaded once at startup and reused for every document in the batch.

    Examples:
      # Process all tariff sheets on GPU (discovery records source)
      duke-rates doc-intel process-docling-batch --accelerator cuda --classification tariff_sheets

      # Process the 322 OCR-remediation historical docs on GPU (scanned, no text)
      duke-rates doc-intel process-docling-batch --accelerator cuda --ocr-remediation

      # Dry run to see what would be processed
      duke-rates doc-intel process-docling-batch --dry-run --limit 20

      # Process up to 100 historical docs on CPU
      duke-rates doc-intel process-docling-batch --source historical --accelerator cpu --limit 100
    """
    from duke_rates.historical.ncuc.pipeline.docling_backend import (
        PIPELINE_STANDARD, convert_pdf_safe, get_docling_unavailable_reason,
        DOCLING_BACKEND_VERSION,
    )
    from duke_rates.db.sqlite import connect
    from duke_rates.hardware.cpu_config import configure_cpu, configure_torch_inference, warmup_gpu

    if not dry_run:
        unavailable = get_docling_unavailable_reason()
        if unavailable:
            typer.echo(f"Docling unavailable: {unavailable}")
            raise typer.Exit(code=1)

        configure_cpu()
        configure_torch_inference()
        if accelerator == "cuda":
            typer.echo("Warming up GPU (loads CUDA DLLs once for the entire batch)...")
            warmup_gpu()
            typer.echo("GPU ready.")

    # --ocr-remediation implies --source historical and --scanned
    if ocr_remediation:
        source = "historical"
        scanned = True

    settings, _ = _bootstrap()
    conn = connect(settings.database_path)

    # Cached-success status values stored across different docling_backend versions
    _SUCCESS_STATUSES = "('success', 'ConversionStatus.SUCCESS', 'ConversionStatus.PARTIAL_SUCCESS', 'partial_success')"

    if source == "historical":
        # Query historical_documents for NC docs that need Docling processing.
        # Deduplicate by local_path — multiple family rows may share the same PDF.
        dedup_conditions = "hd.state = 'NC' AND hd.local_path IS NOT NULL"
        params: list = []

        cache_filter = ""
        if not force:
            cache_filter = f"""
              AND NOT EXISTS (
                SELECT 1 FROM docling_artifacts a
                WHERE a.source_pdf = hd.local_path
                  AND a.backend_version = ?
                  AND a.accelerator = ?
                  AND a.status IN {_SUCCESS_STATUSES}
              )
            """
            params.extend([DOCLING_BACKEND_VERSION, accelerator])

        ocr_filter = ""
        if ocr_remediation:
            if not force:
                # When doing OCR remediation, a "successful" docling run that
                # didn't produce usable raw text isn't really done — the doc
                # still has raw_text_path = NULL.  Override the cache_filter
                # so it only skips docs that have BOTH a cached artifact AND
                # usable raw text on disk.
                cache_filter = f"""
                  AND NOT (
                    EXISTS (
                      SELECT 1 FROM docling_artifacts a
                      WHERE a.source_pdf = hd.local_path
                        AND a.backend_version = ?
                        AND a.accelerator = ?
                        AND a.status IN {_SUCCESS_STATUSES}
                    )
                    AND (hd.raw_text_path IS NOT NULL AND hd.raw_text_path != '')
                  )
                """
            # Target docs with no usable raw text OR weak_layout_sensitive route.
            # The first branch catches docs the OCR pipeline never produced text for.
            # The second branch catches docs that DO have text but the latest parser
            # outcome was weak/empty on a layout-heavy page set — these benefit from
            # Docling's structure-aware re-conversion even though they have raw text.
            #
            # We intentionally do NOT filter on ncuc_page_artifacts: many candidates
            # have noisy page artifacts from prior CPU Docling runs but raw_text_path
            # is still NULL. Use --force to also skip the docling_artifacts cache.
            ocr_filter = """
              AND (
                (hd.raw_text_path IS NULL OR hd.raw_text_path = '')
                OR EXISTS (
                  SELECT 1 FROM v_document_diagnostics vd
                  WHERE vd.historical_document_id = hd.id
                    AND vd.route_reason = 'weak_layout_sensitive'
                )
              )
            """

        query = f"""
            SELECT MIN(hd.id) AS id, hd.local_path,
                   MIN(hd.content_hash) AS content_hash,
                   NULL AS filing_classification,
                   MIN(hd.raw_text_path) AS raw_text_path,
                   MIN(hd.family_key) AS family_key
            FROM historical_documents hd
            WHERE {dedup_conditions}
            {cache_filter}
            {ocr_filter}
            GROUP BY hd.local_path
            ORDER BY MIN(hd.id) ASC
        """
    else:
        # Default: query ncuc_discovery_records
        query = """
            SELECT r.id, r.local_path, r.content_hash, r.filing_classification,
                   NULL AS raw_text_path, NULL AS family_key
            FROM ncuc_discovery_records r
            WHERE r.local_path IS NOT NULL
        """
        params = []

        if not force:
            query += f"""
              AND NOT EXISTS (
                SELECT 1 FROM docling_artifacts a
                WHERE a.source_pdf = r.local_path
                  AND a.backend_version = ?
                  AND a.accelerator = ?
                  AND a.status IN {_SUCCESS_STATUSES}
              )
            """
            params.extend([DOCLING_BACKEND_VERSION, accelerator])

        if classification:
            query += " AND r.filing_classification = ?"
            params.append(classification)

        query += " ORDER BY r.file_size_bytes ASC"  # smallest first — quicker wins early

    if limit > 0:
        query += f" LIMIT {limit}"

    rows = conn.execute(query, params).fetchall()
    total = len(rows)

    if total == 0:
        # Surface a diagnostic instead of leaving the operator guessing why the
        # batch is empty. Distinguishes "filtered to nothing" from "all already done".
        hints: list[str] = []
        if not force:
            hints.append("--force re-runs already-processed docs (current default skips them)")
        if classification:
            hints.append(f"--classification={classification!r} filter is active; remove to widen")
        if ocr_remediation:
            hints.append("--ocr-remediation restricts to the run_docling_or_paddle_structure lane; check `ocr show-remediation-candidates-nc`")
        if source == "historical":
            hints.append("source=historical only sees docs with hd.local_path set; check lineage show-fingerprint-coverage-nc")
        elif source == "discovery":
            hints.append("source=discovery requires ncuc_discovery_records rows; check ncuc list")
        if scanned:
            hints.append("--scanned restricts to docs flagged scanned=True")
        typer.echo(
            f"No documents matched the batch filters "
            f"[source={source}, scanned={scanned}, ocr_remediation={ocr_remediation}, "
            f"classification={classification or '(any)'}, force={force}]."
        )
        if hints:
            typer.echo("Possible reasons:")
            for hint in hints:
                typer.echo(f"  - {hint}")
        conn.close()
        return

    if dry_run:
        typer.echo(f"Would process {total} document(s) [source={source}, scanned={scanned}, ocr_remediation={ocr_remediation}]:")
        for r in rows[:50]:
            label = r["filing_classification"] or r["family_key"] or "unknown"
            typer.echo(f"  [{label}] {r['local_path']}")
        if total > 50:
            typer.echo(f"  ... and {total - 50} more")
        conn.close()
        return

    typer.echo(f"Processing {total} document(s) with accelerator={accelerator} source={source} scanned={scanned}")
    typer.echo("Press Ctrl+C to stop — progress is committed after each document.\n")

    done = 0
    failed = 0
    skipped = 0

    try:
        for i, row in enumerate(rows, 1):
            pdf_path = row["local_path"]
            record_id = row["id"]

            if not __import__("pathlib").Path(pdf_path).exists():
                typer.echo(f"  [{i}/{total}] SKIP (missing): {pdf_path}")
                skipped += 1
                continue

            typer.echo(f"  [{i}/{total}] {pdf_path}", nl=False)

            import time as _time
            t0 = _time.perf_counter()

            # historical docs don't have a discovery_record FK — pass None; artifact
            # is still keyed by source_pdf so doc-intel mine-docling can pick it up
            discovery_record_id = record_id if source == "discovery" else None

            result = convert_pdf_safe(
                pdf_path,
                accelerator=accelerator,
                force=force,
                has_scanned_pages=scanned,
                conn=conn,
                discovery_record_id=discovery_record_id,
            )

            elapsed = _time.perf_counter() - t0

            if result:
                conn.commit()
                tables_count = len(result.get("tables") or [])
                degraded = result.get("_degraded_modes")
                skipped = result.get("_skipped_pages", [])
                suffix = ""
                if degraded:
                    suffix = f" [{','.join(degraded)}]"
                if skipped:
                    suffix += f" (skipped {len(skipped)}p)"
                typer.echo(
                    f"  OK  pages={result['page_count']} tables={tables_count} "
                    f"t={elapsed:.1f}s{suffix}"
                )
                done += 1
            else:
                typer.echo(f"  FAIL  t={elapsed:.1f}s")
                failed += 1

    except KeyboardInterrupt:
        typer.echo("\nInterrupted — committing progress.")
        conn.commit()

    conn.close()
    typer.echo(f"\nDone: {done} converted, {failed} failed, {skipped} skipped of {total} total.")


@doc_intel_app.command("audit-document-type-classifications")
def audit_document_type_classifications_nc(
    state: str = typer.Option("NC", "--state", help="State filter for historical_documents."),
    export_gold_set: Path | None = typer.Option(
        None,
        "--export-gold-set",
        help=(
            "If set, write high-agreement docs as a JSONL gold-set candidate "
            "file. One row per doc: {hd_id, label, confidence, classifiers, "
            "title, text_sample, family_key, state}. The text_sample is the "
            "first 2000 chars from the bulk extractor's text path."
        ),
    ),
    min_classifiers: int = typer.Option(
        2,
        "--min-classifiers",
        help="Minimum classifiers that must have run for a gold-set candidate.",
    ),
    require_unanimous: bool = typer.Option(
        True,
        "--require-unanimous/--allow-majority",
        help=(
            "Gold-set candidates require unanimous label agreement across "
            "all running classifiers (default). With --allow-majority, "
            "any doc whose >=50%% of classifiers agree qualifies."
        ),
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON summary."),
) -> None:
    """Audit document_type classifier agreement and (optionally) export a gold set.

    Surfaces three buckets:
      - Gold-set candidates: docs where multiple classifiers agree on the
        same label. The starter training set for fine-tuning a small
        classifier or seeding human review.
      - Disagreement docs: docs where classifiers split. The highest-
        leverage targets for hand labeling — they're the cases the current
        rule/embedding/LLM stack can't decide on its own.
      - Coverage gaps: docs missing one or more classifiers (LLM never ran,
        embedding never ran). Backfilling these improves the agreement
        signal corpus-wide.

    Per the Stream A direction in docs/research/document_identification.md.
    """
    import sqlite3
    from collections import Counter, defaultdict

    settings, _ = _bootstrap()
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Pull all document_type classifications for state-filtered hd's
    c.execute(
        """
        SELECT dc.subject_id AS hd_id_str,
               dc.classifier,
               dc.label,
               dc.confidence,
               hd.family_key,
               hd.title,
               hd.state
        FROM document_classifications dc
        JOIN historical_documents hd
          ON CAST(hd.id AS TEXT) = dc.subject_id
         AND dc.subject_kind = 'historical_document'
        WHERE dc.stage = 'document_type'
          AND hd.state = ?
        """,
        (state,),
    )
    rows = c.fetchall()

    # Group by doc
    by_doc: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_doc[r["hd_id_str"]].append(r)

    # Per-doc agreement analysis
    gold_candidates: list[dict] = []
    disagreement: list[dict] = []
    coverage_gaps: list[dict] = []

    classifier_universe = {"rule_document_type_v1", "embedding_knn_v1"}  # llm runs are spotty
    for hd_id_str, doc_rows in by_doc.items():
        classifiers_present = {r["classifier"] for r in doc_rows}
        labels = [r["label"] for r in doc_rows]
        label_counter = Counter(labels)
        most_common_label, most_common_n = label_counter.most_common(1)[0]
        n_classifiers = len(classifiers_present)
        n_distinct_labels = len(label_counter)

        title = doc_rows[0]["title"]
        family_key = doc_rows[0]["family_key"]

        # Gold-set membership rule
        if n_classifiers >= min_classifiers:
            unanimous = n_distinct_labels == 1
            majority = most_common_n / n_classifiers >= 0.5
            qualifies = unanimous if require_unanimous else majority
            if qualifies:
                # Average confidence of voters for the winning label
                voters = [r for r in doc_rows if r["label"] == most_common_label]
                avg_conf = sum(r["confidence"] for r in voters) / max(1, len(voters))
                gold_candidates.append({
                    "hd_id": int(hd_id_str),
                    "label": most_common_label,
                    "confidence": round(avg_conf, 3),
                    "classifiers": sorted(classifiers_present),
                    "votes_for_label": most_common_n,
                    "total_classifiers": n_classifiers,
                    "family_key": family_key,
                    "title": title,
                })

        if n_distinct_labels >= 2 and n_classifiers >= 2:
            disagreement.append({
                "hd_id": int(hd_id_str),
                "labels": dict(label_counter),
                "classifiers": sorted(classifiers_present),
                "family_key": family_key,
                "title": title,
            })

        missing = classifier_universe - classifiers_present
        if missing:
            coverage_gaps.append({
                "hd_id": int(hd_id_str),
                "present": sorted(classifiers_present),
                "missing": sorted(missing),
                "family_key": family_key,
            })

    # Classifier-wide confidence stats
    c.execute(
        """
        SELECT classifier,
               COUNT(*) AS n,
               AVG(confidence) AS avg_c,
               MIN(confidence) AS min_c,
               MAX(confidence) AS max_c,
               SUM(CASE WHEN confidence >= 0.9 THEN 1 ELSE 0 END) AS hi,
               SUM(CASE WHEN confidence < 0.5 THEN 1 ELSE 0 END) AS lo
        FROM document_classifications dc
        JOIN historical_documents hd
          ON CAST(hd.id AS TEXT) = dc.subject_id
         AND dc.subject_kind = 'historical_document'
        WHERE dc.stage = 'document_type' AND hd.state = ?
        GROUP BY classifier
        """,
        (state,),
    )
    classifier_stats = [dict(r) for r in c.fetchall()]

    summary = {
        "state": state,
        "docs_with_any_classification": len(by_doc),
        "gold_set_candidates": len(gold_candidates),
        "disagreement_docs": len(disagreement),
        "coverage_gaps": len(coverage_gaps),
        "classifier_stats": classifier_stats,
        "label_distribution": dict(Counter(
            g["label"] for g in gold_candidates
        ).most_common()),
    }

    if json_out:
        typer.echo(json.dumps(summary, indent=2))
        if export_gold_set:
            _write_gold_set_jsonl(export_gold_set, gold_candidates, conn, settings)
        conn.close()
        return

    typer.echo(f"\nDocument-type classification audit | state={state}\n")
    typer.echo(f"  docs with any classification:  {summary['docs_with_any_classification']}")
    typer.echo(f"  gold-set candidates:           {summary['gold_set_candidates']}")
    typer.echo(f"  disagreement docs:             {summary['disagreement_docs']}")
    typer.echo(f"  coverage gaps:                 {summary['coverage_gaps']}")

    typer.echo("\n  Per-classifier confidence:")
    typer.echo(f"    {'classifier':<35} {'n':>5} {'avg':>6} {'min':>6} {'max':>6} {'hi(>=0.9)':>10} {'lo(<0.5)':>9}")
    for stat in classifier_stats:
        typer.echo(
            f"    {stat['classifier']:<35} {stat['n']:>5} "
            f"{stat['avg_c']:>6.2f} {stat['min_c']:>6.2f} {stat['max_c']:>6.2f} "
            f"{stat['hi']:>10} {stat['lo']:>9}"
        )

    typer.echo("\n  Gold-set candidates by label:")
    for label, n in summary["label_distribution"].items():
        typer.echo(f"    {label:<30} {n}")

    if export_gold_set:
        written = _write_gold_set_jsonl(export_gold_set, gold_candidates, conn, settings)
        typer.echo(f"\n  Wrote {written} gold-set rows to {export_gold_set}")
    else:
        typer.echo(
            "\n  Pass --export-gold-set PATH.jsonl to write a JSONL training "
            "candidate file."
        )

    conn.close()


@doc_intel_app.command("seed-document-type-gold")
def seed_document_type_gold_nc(
    state: str = typer.Option("NC", "--state", help="State filter for historical_documents."),
    min_classifiers: int = typer.Option(
        2,
        "--min-classifiers",
        help=(
            "Minimum number of classifiers that must have run AND agreed on "
            "a single label for the doc to seed gold. 2 = relaxed (rule + "
            "embedding agree), 3 = strict (rule + embedding + LLM all agree)."
        ),
    ),
    exclude_classifiers: list[str] | None = typer.Option(
        None,
        "--exclude-classifier",
        help=(
            "Repeatable. Skip these classifiers when computing agreement. "
            "Useful when seeding pre-v2 gold to establish a baseline before "
            "the new classifier enters the vote."
        ),
    ),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Actually write gold rows. Without this flag, dry-run only.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit summary as JSON."),
) -> None:
    """Seed document_type_gold from classifier agreement.

    Default rule: a doc seeds gold when at least ``min_classifiers``
    classifiers have run AND they all agree on a single label. The
    inserted row carries:

      label       = the agreed-upon label
      labeler     = 'agreement:<classifier1>+<classifier2>+...'
      source      = 'unanimous_classifier_agreement'
      evidence    = {classifiers: [...], confidences: [...]}

    Idempotent: docs that already have an active (non-superseded) gold
    row are skipped. To force-rewrite, supersede the existing row first
    via a follow-up command.

    Use ``--exclude-classifier rule_document_type_v2`` to seed gold
    based on the pre-v2 classifier stack — recommended for first
    seeding so v2's known biases don't contaminate the baseline.
    """
    import sqlite3
    from collections import Counter, defaultdict

    settings, _ = _bootstrap()
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    exclude_set = set(exclude_classifiers or [])

    # Pull all document_type classifications for the state
    c.execute(
        """
        SELECT dc.subject_id, dc.classifier, dc.label, dc.confidence
        FROM document_classifications dc
        JOIN historical_documents hd
          ON CAST(hd.id AS TEXT) = dc.subject_id
         AND dc.subject_kind = 'historical_document'
        WHERE dc.stage = 'document_type'
          AND hd.state = ?
        """,
        (state,),
    )

    by_doc: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in c.fetchall():
        if r["classifier"] in exclude_set:
            continue
        by_doc[r["subject_id"]].append(r)

    # Find docs with existing active gold rows (to skip)
    c.execute(
        """
        SELECT subject_id FROM document_type_gold
        WHERE subject_kind = 'historical_document' AND superseded_by IS NULL
        """
    )
    existing_gold = {r["subject_id"] for r in c.fetchall()}

    seeded: list[dict] = []
    skipped_already_gold = 0
    skipped_too_few_classifiers = 0
    skipped_disagreement = 0
    label_counts: Counter = Counter()

    for subj_id, rows in by_doc.items():
        if subj_id in existing_gold:
            skipped_already_gold += 1
            continue
        if len(rows) < min_classifiers:
            skipped_too_few_classifiers += 1
            continue
        labels = [r["label"] for r in rows]
        if len(set(labels)) != 1:
            skipped_disagreement += 1
            continue

        agreed_label = labels[0]
        classifiers = sorted(r["classifier"] for r in rows)
        confidences = [round(r["confidence"], 3) for r in rows]
        labeler = "agreement:" + "+".join(classifiers)
        evidence = {
            "classifiers": classifiers,
            "confidences": confidences,
            "avg_confidence": round(sum(confidences) / len(confidences), 3),
        }
        seeded.append({
            "subject_id": subj_id,
            "label": agreed_label,
            "labeler": labeler,
            "evidence": evidence,
            "classifier_count": len(rows),
        })
        label_counts[agreed_label] += 1

    if execute and seeded:
        from datetime import UTC as _UTC
        utc_now = datetime.now(_UTC).isoformat()
        for row in seeded:
            c.execute(
                """
                INSERT INTO document_type_gold (
                    subject_kind, subject_id, label, labeler, source,
                    evidence_json, superseded_by, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    "historical_document",
                    row["subject_id"],
                    row["label"],
                    row["labeler"],
                    "unanimous_classifier_agreement",
                    json.dumps(row["evidence"]),
                    utc_now,
                ),
            )
        conn.commit()

    summary = {
        "state": state,
        "candidates_considered": len(by_doc),
        "seeded": len(seeded),
        "skipped_already_gold": skipped_already_gold,
        "skipped_too_few_classifiers": skipped_too_few_classifiers,
        "skipped_disagreement": skipped_disagreement,
        "min_classifiers": min_classifiers,
        "exclude_classifiers": sorted(exclude_set),
        "label_distribution": dict(label_counts.most_common()),
        "executed": execute,
    }

    if json_out:
        typer.echo(json.dumps(summary, indent=2))
        conn.close()
        return

    typer.echo(f"\ndocument_type_gold seeding | state={state}\n")
    typer.echo(f"  min_classifiers required:       {min_classifiers}")
    if exclude_set:
        typer.echo(f"  excluded classifiers:           {sorted(exclude_set)}")
    typer.echo(f"  docs considered:                {summary['candidates_considered']}")
    typer.echo(f"  -> already gold (skipped):      {summary['skipped_already_gold']}")
    typer.echo(f"  -> too few classifiers:         {summary['skipped_too_few_classifiers']}")
    typer.echo(f"  -> classifier disagreement:     {summary['skipped_disagreement']}")
    typer.echo(f"  -> would seed:                  {summary['seeded']}")
    if label_counts:
        typer.echo("\n  Seeded label distribution:")
        for label, n in label_counts.most_common():
            typer.echo(f"    {label:<28} {n}")
    if execute:
        typer.echo(f"\n  Wrote {len(seeded)} new gold rows to document_type_gold.")
    else:
        typer.echo("\n  Dry-run only. Pass --execute to write gold rows.")

    conn.close()


@doc_intel_app.command("train-document-type-baseline")
def train_document_type_baseline_nc(
    state: str = typer.Option("NC", "--state", help="State filter."),
    val_fraction: float = typer.Option(
        0.2, "--val-fraction",
        help="Stratified val split fraction for classes with >=5 samples.",
    ),
    random_state: int = typer.Option(
        13, "--random-state", help="Deterministic seed for the val split."
    ),
    save_path: Path | None = typer.Option(
        None,
        "--save",
        help=(
            "Optional path to joblib-dump the fitted (vectorizer, model) "
            "tuple for reuse. Recommend models/baseline_document_type.joblib."
        ),
    ),
    cv_folds: int = typer.Option(
        0,
        "--cv",
        help=(
            "If > 1, also run stratified k-fold CV with this many folds and "
            "print mean/std accuracy + F1. Recommended k=5. CV is more honest "
            "than the single train/val split when the gold set is small "
            "(~441 rows produces 3-5 pts of accuracy drift between random "
            "seeds on a single split)."
        ),
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit metrics as JSON."),
) -> None:
    """Stream D baseline: TF-IDF + LogisticRegression on document_type_gold.

    Sets a measurable starting point for any later fine-tuned model
    (DistilBERT, qwen-finetuned) to compare against. Pulls all active
    gold rows for the given state, materializes their text samples via
    the same path the bulk extractor uses, splits stratified train/val
    (with rare classes pinned to train), and fits a multi-class logistic
    regression with class_weight='balanced'.

    Per docs/research/document_identification.md Stream D — this is the
    intentionally-minimal first cut. See the module-level docstring of
    duke_rates.classification.baseline_classifier for rationale.
    """
    import sqlite3
    from duke_rates.classification.baseline_classifier import (
        TrainingDataset, train_baseline, cross_validate_baseline,
    )
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import (
        BulkExtractor, normalize_docling_markdown, normalize_ocr_text,
    )

    settings, _ = _bootstrap()
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute(
        """
        SELECT dtg.subject_id, dtg.label
        FROM document_type_gold dtg
        JOIN historical_documents hd
          ON CAST(hd.id AS TEXT) = dtg.subject_id
         AND dtg.subject_kind = 'historical_document'
        WHERE dtg.superseded_by IS NULL AND hd.state = ?
        """,
        (state,),
    )
    gold_rows = c.fetchall()
    if not gold_rows:
        typer.echo(f"No gold rows for state={state}. Seed gold first.", err=True)
        raise typer.Exit(1)

    typer.echo(f"Loading text for {len(gold_rows)} gold docs...")
    extractor = BulkExtractor(db_path=str(settings.database_path))
    hd_ids: list[int] = []
    labels: list[str] = []
    texts: list[str] = []
    missing = 0
    for r in gold_rows:
        hd_id = int(r["subject_id"])
        doc = extractor.get_document_for_extraction(hd_id)
        if not doc:
            missing += 1
            continue
        try:
            text, src = extractor.extract_text_from_pdf(
                doc["local_path"],
                start_page=doc.get("start_page"),
                end_page=doc.get("end_page"),
            )
            if src in ("docling_artifact", "docling_artifact_sliced"):
                text = normalize_docling_markdown(text)
            text = normalize_ocr_text(text)
        except Exception:
            text = ""
        if not text:
            missing += 1
            continue
        hd_ids.append(hd_id)
        labels.append(r["label"])
        texts.append(text[:2000])  # Match the text_sample slice the seeders use

    conn.close()

    if not texts:
        typer.echo("No text recoverable for any gold doc.", err=True)
        raise typer.Exit(1)

    typer.echo(
        f"Materialized {len(texts)} rows ({missing} skipped — no text). "
        f"Training baseline..."
    )

    dataset = TrainingDataset(hd_ids=hd_ids, labels=labels, texts=texts)
    result = train_baseline(
        dataset, val_fraction=val_fraction, random_state=random_state
    )

    metrics = {
        "state": state,
        "gold_rows_loaded": len(gold_rows),
        "rows_used": len(texts),
        "skipped_no_text": missing,
        "classes": result.classes,
        "train_n": result.train_n,
        "val_n": result.val_n,
        "train_only_classes": result.train_only_classes,
        "val_accuracy": round(result.val_accuracy, 4),
        "overall_train_accuracy": round(result.overall_train_accuracy, 4),
        "per_class": {
            lab: {
                "precision": round(stats.get("precision", 0.0), 3),
                "recall": round(stats.get("recall", 0.0), 3),
                "f1-score": round(stats.get("f1-score", 0.0), 3),
                "support": int(stats.get("support", 0)),
            }
            for lab, stats in result.val_classification_report.items()
            if isinstance(stats, dict) and lab not in ("accuracy",)
        },
    }

    if save_path:
        import joblib
        save_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {"vectorizer": result.vectorizer, "model": result.model,
             "classes": result.classes, "metrics": metrics},
            save_path,
        )
        metrics["saved_to"] = str(save_path)

    # Optional cross-validation pass for a more honest accuracy number
    cv_metrics: dict | None = None
    if cv_folds and cv_folds >= 2:
        cv_result = cross_validate_baseline(
            dataset, n_folds=cv_folds, random_state=random_state
        )
        cv_metrics = {
            "n_folds": cv_result.n_folds,
            "eligible_rows": cv_result.eligible_n,
            "train_only_classes": cv_result.train_only_classes,
            "fold_accuracies": cv_result.fold_accuracies,
            "fold_weighted_f1": cv_result.fold_weighted_f1,
            "fold_macro_f1": cv_result.fold_macro_f1,
            "mean_accuracy": cv_result.mean_accuracy,
            "std_accuracy": cv_result.std_accuracy,
            "mean_weighted_f1": cv_result.mean_weighted_f1,
            "std_weighted_f1": cv_result.std_weighted_f1,
            "mean_macro_f1": cv_result.mean_macro_f1,
            "std_macro_f1": cv_result.std_macro_f1,
        }
        metrics["cross_validation"] = cv_metrics

    if json_out:
        typer.echo(json.dumps(metrics, indent=2))
        return

    typer.echo(f"\nbaseline trained | state={state}\n")
    typer.echo(f"  rows used:                 {metrics['rows_used']}  ({missing} skipped no-text)")
    typer.echo(f"  train rows:                {result.train_n}")
    typer.echo(f"  val rows:                  {result.val_n}")
    typer.echo(f"  classes:                   {len(result.classes)}")
    typer.echo(f"  train-only classes (rare): {result.train_only_classes}")
    typer.echo(f"  val accuracy:              {result.val_accuracy:.3f}")
    typer.echo(f"  train accuracy (ref):      {result.overall_train_accuracy:.3f}")
    typer.echo("\n  Per-class val metrics:")
    typer.echo(f"    {'class':<28} {'P':>5}  {'R':>5}  {'F1':>5}  {'n':>4}")
    per_class = metrics["per_class"]
    # Sort: actual labels first (alphabetic), then macro/weighted averages
    label_keys = sorted(
        k for k in per_class
        if k not in ("macro avg", "weighted avg")
    )
    for lab in label_keys + ["macro avg", "weighted avg"]:
        if lab not in per_class:
            continue
        s = per_class[lab]
        typer.echo(
            f"    {lab:<28} {s['precision']:>5.2f}  {s['recall']:>5.2f}  "
            f"{s['f1-score']:>5.2f}  {s['support']:>4}"
        )
    if save_path:
        typer.echo(f"\n  Artifacts saved to: {save_path}")

    if cv_metrics:
        typer.echo(f"\n  Cross-validation ({cv_metrics['n_folds']}-fold, "
                   f"{cv_metrics['eligible_rows']} eligible rows):")
        typer.echo(f"    accuracy:    mean={cv_metrics['mean_accuracy']:.4f} "
                   f"std={cv_metrics['std_accuracy']:.4f}  "
                   f"folds={cv_metrics['fold_accuracies']}")
        typer.echo(f"    weighted F1: mean={cv_metrics['mean_weighted_f1']:.4f} "
                   f"std={cv_metrics['std_weighted_f1']:.4f}")
        typer.echo(f"    macro F1:    mean={cv_metrics['mean_macro_f1']:.4f} "
                   f"std={cv_metrics['std_macro_f1']:.4f}")
        if cv_metrics['train_only_classes']:
            typer.echo(
                f"    train-only classes (n<{cv_metrics['n_folds']} samples): "
                f"{cv_metrics['train_only_classes']}"
            )


@doc_intel_app.command("audit-stale-gold")
def audit_stale_gold_nc(
    state: str = typer.Option("NC", "--state", help="State filter."),
    min_v2_confidence: float = typer.Option(
        0.9, "--min-v2-confidence",
        help="Only flag rows where v2 disagrees at >= this confidence.",
    ),
    mark_for_review: bool = typer.Option(
        False,
        "--mark-for-review",
        help=(
            "Append a 'v2 disagrees: <label>@<conf>' annotation to the gold "
            "row's notes field. Does NOT supersede the row — human review "
            "decides whether to keep the original label, replace it, or "
            "split (treat both as valid for a mixed-content bundle)."
        ),
    ),
    out: Path | None = typer.Option(
        None, "--out",
        help=(
            "Optional JSONL path. One row per stale gold doc with the "
            "original gold label, v2's disagreement label, v2 confidence, "
            "and all classifier votes for context."
        ),
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit summary as JSON."),
) -> None:
    """Find document_type_gold rows where the v2 classifier now disagrees.

    The v0 gold set was seeded from rule_v1 + embedding (+ optional LLM)
    agreement. After v2 backfilled corpus-wide, v2 disagrees with many
    of those agreements. The gold rows weren't auto-superseded — they
    remain as point-in-time labels — but those still-valid labels
    should be sanity-checked given v2's higher confidence.

    Common patterns the live corpus surfaces:
    - TESTIMONY -> COVER_LETTER: PDF body opens with a transmittal
      letter rather than direct testimony; the original label saw
      the testimony content but rule_v1 didn't distinguish well.
    - ORDER_FINAL -> TARIFF_SHEET / RIDER: leaf-revision orders that
      include the new tariff body; v2 reads the tariff section.
    - TESTIMONY/ORDER_FINAL -> RIDER: a rider's filing testimony or
      approval order whose body content reads as the rider itself.

    Read-only by default. --mark-for-review writes a notes annotation
    so reviewers can pick the row up from a future label-fix UI. No
    other DB writes; supersession requires an explicit follow-up.
    """
    import sqlite3
    from collections import Counter, defaultdict

    settings, _ = _bootstrap()
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute(
        """
        WITH v2 AS (
          SELECT subject_id, label AS v2_label, confidence AS v2_confidence,
                 evidence_json AS v2_evidence
          FROM document_classifications
          WHERE stage='document_type'
            AND classifier='rule_document_type_v2'
        )
        SELECT
            CAST(hd.id AS INTEGER) AS hd_id,
            hd.family_key, hd.title,
            dtg.id AS gold_id,
            dtg.label AS gold_label,
            dtg.labeler AS gold_labeler,
            dtg.source AS gold_source,
            dtg.notes AS gold_notes,
            v2.v2_label, v2.v2_confidence, v2.v2_evidence
        FROM document_type_gold dtg
        JOIN historical_documents hd
          ON CAST(hd.id AS TEXT) = dtg.subject_id
        JOIN v2 ON v2.subject_id = dtg.subject_id
        WHERE dtg.superseded_by IS NULL
          AND dtg.subject_kind = 'historical_document'
          AND hd.state = ?
          AND v2.v2_label != dtg.label
          AND v2.v2_confidence >= ?
        ORDER BY v2.v2_confidence DESC, hd.id
        """,
        (state, min_v2_confidence),
    )
    rows = [dict(r) for r in c.fetchall()]

    # Group by (gold_label, v2_label)
    pairs: Counter = Counter()
    by_gold: Counter = Counter()
    by_v2: Counter = Counter()
    for r in rows:
        pairs[(r["gold_label"], r["v2_label"])] += 1
        by_gold[r["gold_label"]] += 1
        by_v2[r["v2_label"]] += 1

    summary = {
        "state": state,
        "min_v2_confidence": min_v2_confidence,
        "total_stale": len(rows),
        "by_gold_label": dict(by_gold.most_common()),
        "by_v2_label": dict(by_v2.most_common()),
        "top_pairs": [
            {"gold_label": g, "v2_label": v2, "count": n}
            for (g, v2), n in pairs.most_common(15)
        ],
        "marked_for_review": 0,
    }

    if mark_for_review and rows:
        from datetime import UTC as _UTC
        utc_now = datetime.now(_UTC).isoformat()
        for r in rows:
            annotation = (
                f"v2 disagrees: {r['v2_label']}@{r['v2_confidence']:.2f} "
                f"(audited {utc_now[:10]})"
            )
            existing_notes = r["gold_notes"] or ""
            # Idempotency — don't append if the annotation is already there
            if annotation in existing_notes:
                continue
            new_notes = (existing_notes + "\n" + annotation).strip()
            c.execute(
                "UPDATE document_type_gold SET notes = ? WHERE id = ?",
                (new_notes, r["gold_id"]),
            )
            summary["marked_for_review"] += 1
        conn.commit()

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        summary["written_to"] = str(out)

    conn.close()

    if json_out:
        typer.echo(json.dumps(summary, indent=2))
        return

    typer.echo(f"\nStale-gold audit | state={state}")
    typer.echo(f"  v2 min confidence:        {min_v2_confidence}")
    typer.echo(f"  total stale rows:         {len(rows)}")

    typer.echo("\n  By gold label (what v0 said):")
    for label, n in by_gold.most_common():
        typer.echo(f"    {label:<28} {n}")

    typer.echo("\n  By v2 label (what v2 says now):")
    for label, n in by_v2.most_common():
        typer.echo(f"    {label:<28} {n}")

    typer.echo("\n  Top stale pairs (gold -> v2):")
    typer.echo(f"    {'gold label':<26} -> {'v2 label':<26} {'n':>4}")
    for (g, v2), n in pairs.most_common(10):
        typer.echo(f"    {g:<26} -> {v2:<26} {n:>4}")

    if mark_for_review:
        typer.echo(f"\n  Marked {summary['marked_for_review']} gold rows for review (notes annotation).")
    if out:
        typer.echo(f"  Per-doc JSONL written to: {out}")
    elif not mark_for_review:
        typer.echo("\n  Dry-run only. Pass --mark-for-review to annotate notes, or --out to export JSONL.")


@doc_intel_app.command("audit-bundle-metadata-mismatch")
def audit_bundle_metadata_mismatch_nc(
    state: str = typer.Option("NC", "--state", help="State filter."),
    min_confidence: float = typer.Option(
        0.9, "--min-confidence",
        help="Minimum v2 confidence required to flag a mismatch.",
    ),
    out: Path | None = typer.Option(
        None, "--out",
        help=(
            "Optional JSONL path. One row per mismatched doc with v2 label, "
            "family_key, title, and the full v2 evidence so a reviewer can "
            "decide whether to re-tag the family_key or accept that the "
            "bundle wraps mixed content."
        ),
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit summary as JSON."),
) -> None:
    """Find docs where v2's content classification disagrees with the importer's family_key tag.

    When v2 classifies a doc as a non-tariff type (COVER_LETTER, ORDER_FINAL,
    APPLICATION, COMPLIANCE_FILING, CERTIFICATE_OF_SERVICE, NOTICE_OF_HEARING,
    TESTIMONY) but its family_key implies a tariff family (nc-progress-leaf-*,
    nc-carolinas-schedule-*, nc-carolinas-rider-*), that's a strong signal
    the importer tagged the wrong family. The PDF body is the cover letter
    or order transmitting the tariff, not the tariff itself.

    See docs/research/document_identification.md "Cover-letter bundle signal"
    section. This CLI quantifies the surface and exports a triage queue.
    Read-only — no DB writes. Cleanup decisions are out of scope.
    """
    import sqlite3
    from collections import Counter, defaultdict

    settings, _ = _bootstrap()
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    ADMIN_TYPES = (
        "COVER_LETTER", "ORDER_FINAL", "ORDER_PROCEDURAL",
        "APPLICATION", "COMPLIANCE_FILING",
        "CERTIFICATE_OF_SERVICE", "NOTICE_OF_HEARING", "TESTIMONY",
    )
    TARIFF_FAMILY_PREFIXES = (
        "nc-progress-leaf-",
        "nc-carolinas-schedule-",
        "nc-carolinas-rider-",
    )

    placeholders = ",".join("?" for _ in ADMIN_TYPES)
    family_clauses = " OR ".join(
        "hd.family_key LIKE ?" for _ in TARIFF_FAMILY_PREFIXES
    )
    like_args = [p + "%" for p in TARIFF_FAMILY_PREFIXES]

    c.execute(
        f"""
        SELECT
            CAST(hd.id AS INTEGER) AS hd_id,
            hd.family_key,
            hd.title,
            v2.label AS v2_label,
            v2.confidence AS v2_confidence,
            v2.evidence_json AS v2_evidence
        FROM document_classifications v2
        JOIN historical_documents hd
          ON CAST(hd.id AS TEXT) = v2.subject_id
         AND v2.subject_kind = 'historical_document'
        WHERE v2.stage = 'document_type'
          AND v2.classifier = 'rule_document_type_v2'
          AND hd.state = ?
          AND v2.confidence >= ?
          AND v2.label IN ({placeholders})
          AND ({family_clauses})
        ORDER BY v2.confidence DESC, hd.id
        """,
        (state, min_confidence, *ADMIN_TYPES, *like_args),
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    by_v2_label = Counter(r["v2_label"] for r in rows)
    by_family_prefix: Counter = Counter()
    for r in rows:
        for prefix in TARIFF_FAMILY_PREFIXES:
            if r["family_key"].startswith(prefix):
                by_family_prefix[prefix] += 1
                break

    # Pairs (v2_label, family_prefix) — most-mismatched combinations
    pairs: Counter = Counter()
    for r in rows:
        for prefix in TARIFF_FAMILY_PREFIXES:
            if r["family_key"].startswith(prefix):
                pairs[(r["v2_label"], prefix)] += 1
                break

    summary = {
        "state": state,
        "min_confidence": min_confidence,
        "total_mismatches": len(rows),
        "by_v2_label": dict(by_v2_label.most_common()),
        "by_family_prefix": dict(by_family_prefix.most_common()),
        "top_pairs": [
            {"v2_label": lab, "family_prefix": pref, "count": n}
            for (lab, pref), n in pairs.most_common(10)
        ],
    }

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        summary["written_to"] = str(out)

    if json_out:
        typer.echo(json.dumps(summary, indent=2))
        return

    typer.echo(f"\nBundle metadata mismatch audit | state={state}\n")
    typer.echo(f"  v2 min confidence:           {min_confidence}")
    typer.echo(f"  total mismatches:            {len(rows)}")

    typer.echo("\n  By v2 (content) label:")
    for label, n in by_v2_label.most_common():
        typer.echo(f"    {label:<28} {n}")

    typer.echo("\n  By family_key prefix:")
    for prefix, n in by_family_prefix.most_common():
        typer.echo(f"    {prefix:<32} {n}")

    typer.echo("\n  Top mismatched pairs:")
    typer.echo(f"    {'v2 label':<28} {'family prefix':<32} {'n':>4}")
    for (lab, prefix), n in pairs.most_common(10):
        typer.echo(f"    {lab:<28} {prefix:<32} {n:>4}")

    if out:
        typer.echo(f"\n  Per-doc JSONL written to: {out}")
    else:
        typer.echo("\n  Pass --out PATH.jsonl to export per-doc detail.")


@doc_intel_app.command("promote-high-confidence-subset")
def promote_high_confidence_subset_nc(
    state: str = typer.Option("NC", "--state", help="State filter."),
    min_confidence: float = typer.Option(
        0.9,
        "--min-confidence",
        help=(
            "Minimum confidence each subset-agreeing classifier must reach. "
            "0.9 is the recommended floor — LLM/qwen3:8b averages 0.96 and "
            "v2 reaches 0.92+ on strong matches, so 0.9 selects only their "
            "confident calls."
        ),
    ),
    min_subset_size: int = typer.Option(
        2,
        "--min-subset",
        help="Minimum classifiers agreeing on the same label at >= min_confidence.",
    ),
    exclude_classifiers: list[str] | None = typer.Option(
        None,
        "--exclude-classifier",
        help="Repeatable. Skip these classifiers when computing subsets.",
    ),
    execute: bool = typer.Option(
        False, "--execute",
        help="Actually write gold rows. Without this flag, dry-run only.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit summary as JSON."),
) -> None:
    """Promote subset-agreement docs to document_type_gold.

    A subset-agreement is when N classifiers agree on a single label at
    >= min_confidence, even if other classifiers vote differently at
    lower confidence. Useful for growing gold on disagreement docs that
    doc-intel seed-document-type-gold skips (because it requires *unanimous*
    agreement across all running classifiers).

    Concrete pattern this surfaces: LLM qwen3:8b at 1.0 confidence agrees
    with v2 at 0.98 on CERTIFICATE_OF_SERVICE, while v1 and embedding
    vote different labels at lower confidence. The two high-confidence
    classifiers carry the signal; lower-confidence noise is ignored.

    Rows are tagged with:
      labeler  = 'subset:<classifier1>+<classifier2>+...'
      source   = 'high_confidence_subset_agreement'
      evidence = {classifiers, confidences, min_threshold, dissenters}

    Idempotent: docs that already have an active gold row are skipped.

    Recommended workflow:
      1. doc-intel promote-high-confidence-subset                    (dry-run, review)
      2. doc-intel promote-high-confidence-subset --execute          (write to gold)
      3. doc-intel audit-document-type-classifications               (verify growth)
    """
    import sqlite3
    from collections import Counter, defaultdict

    settings, _ = _bootstrap()
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    exclude_set = set(exclude_classifiers or [])

    c.execute(
        """
        SELECT dc.subject_id, dc.classifier, dc.label, dc.confidence
        FROM document_classifications dc
        JOIN historical_documents hd
          ON CAST(hd.id AS TEXT) = dc.subject_id
         AND dc.subject_kind = 'historical_document'
        WHERE dc.stage = 'document_type' AND hd.state = ?
        """,
        (state,),
    )

    by_doc: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in c.fetchall():
        if r["classifier"] in exclude_set:
            continue
        by_doc[r["subject_id"]].append(r)

    c.execute(
        """SELECT subject_id FROM document_type_gold
           WHERE subject_kind='historical_document' AND superseded_by IS NULL"""
    )
    existing_gold = {r["subject_id"] for r in c.fetchall()}

    promoted: list[dict] = []
    skipped_already_gold = 0
    skipped_no_subset = 0
    skipped_subset_disagree = 0
    label_counts: Counter = Counter()

    for subj_id, rows in by_doc.items():
        if subj_id in existing_gold:
            skipped_already_gold += 1
            continue

        # Group HIGH-confidence votes by label
        high_conf_by_label: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in rows:
            if r["confidence"] >= min_confidence:
                high_conf_by_label[r["label"]].append(r)

        # Find the label with the largest high-confidence subset
        if not high_conf_by_label:
            skipped_no_subset += 1
            continue

        best_label = max(
            high_conf_by_label,
            key=lambda lab: len(high_conf_by_label[lab]),
        )
        subset = high_conf_by_label[best_label]
        if len(subset) < min_subset_size:
            skipped_no_subset += 1
            continue

        # Check that no OTHER label has an equally large high-conf subset
        # (that would be a high-conf disagreement, not a clear winner)
        other_max = max(
            (len(v) for lab, v in high_conf_by_label.items() if lab != best_label),
            default=0,
        )
        if other_max >= len(subset):
            skipped_subset_disagree += 1
            continue

        # Build the row
        agreeing_classifiers = sorted(r["classifier"] for r in subset)
        agreeing_confs = [round(r["confidence"], 3) for r in subset]
        dissenters = [
            {
                "classifier": r["classifier"],
                "label": r["label"],
                "confidence": round(r["confidence"], 3),
            }
            for r in rows
            if r["label"] != best_label
        ]
        labeler = "subset:" + "+".join(agreeing_classifiers)
        evidence = {
            "classifiers": agreeing_classifiers,
            "confidences": agreeing_confs,
            "min_threshold": min_confidence,
            "dissenters": dissenters,
        }
        promoted.append({
            "subject_id": subj_id,
            "label": best_label,
            "labeler": labeler,
            "evidence": evidence,
            "subset_size": len(subset),
        })
        label_counts[best_label] += 1

    if execute and promoted:
        from datetime import UTC as _UTC
        utc_now = datetime.now(_UTC).isoformat()
        for row in promoted:
            c.execute(
                """
                INSERT INTO document_type_gold (
                    subject_kind, subject_id, label, labeler, source,
                    evidence_json, superseded_by, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    "historical_document",
                    row["subject_id"],
                    row["label"],
                    row["labeler"],
                    "high_confidence_subset_agreement",
                    json.dumps(row["evidence"]),
                    utc_now,
                ),
            )
        conn.commit()

    summary = {
        "state": state,
        "min_confidence": min_confidence,
        "min_subset_size": min_subset_size,
        "exclude_classifiers": sorted(exclude_set),
        "candidates_considered": len(by_doc),
        "promoted": len(promoted),
        "skipped_already_gold": skipped_already_gold,
        "skipped_no_subset": skipped_no_subset,
        "skipped_subset_disagree": skipped_subset_disagree,
        "label_distribution": dict(label_counts.most_common()),
        "executed": execute,
    }

    if json_out:
        typer.echo(json.dumps(summary, indent=2))
        conn.close()
        return

    typer.echo(f"\nhigh-confidence subset promotion | state={state}\n")
    typer.echo(f"  min_confidence:                 {min_confidence}")
    typer.echo(f"  min subset size:                {min_subset_size}")
    if exclude_set:
        typer.echo(f"  excluded classifiers:           {sorted(exclude_set)}")
    typer.echo(f"  docs considered:                {len(by_doc)}")
    typer.echo(f"  -> already gold:                {skipped_already_gold}")
    typer.echo(f"  -> no qualifying subset:        {skipped_no_subset}")
    typer.echo(f"  -> high-conf disagreement:      {skipped_subset_disagree}")
    typer.echo(f"  -> would promote:               {len(promoted)}")
    if label_counts:
        typer.echo("\n  Promoted label distribution:")
        for label, n in label_counts.most_common():
            typer.echo(f"    {label:<28} {n}")
    if execute:
        typer.echo(f"\n  Wrote {len(promoted)} new gold rows.")
    else:
        typer.echo("\n  Dry-run only. Pass --execute to write.")

    conn.close()


@doc_intel_app.command("triage-disagreements")
def triage_disagreements_nc(
    state: str = typer.Option("NC", "--state", help="State filter."),
    output_path: Path = typer.Option(
        ...,
        "--out",
        help=(
            "Output JSONL path for the labeling queue. Each line is one "
            "disagreement doc with side-by-side classifier votes, layout "
            "signals, and a text sample. Suitable for a notebook or "
            "Streamlit label-fix UI."
        ),
    ),
    limit: int = typer.Option(200, "--limit", help="Cap rows written."),
    weight_underrepresented: bool = typer.Option(
        True,
        "--weight-underrepresented/--no-weight",
        help=(
            "Prioritize docs whose classifiers voted for type buckets that "
            "are underrepresented in document_type_gold. Targets the "
            "specific labels Stream D fine-tuning needs more examples of "
            "(RIDER, COVER_LETTER, NOTICE_OF_HEARING, etc.)."
        ),
    ),
    label_filter: list[str] | None = typer.Option(
        None,
        "--label",
        help=(
            "Repeatable. Only include docs where at least one classifier "
            "voted one of these labels. Use to focus triage on specific "
            "type buckets."
        ),
    ),
) -> None:
    """Export classifier-disagreement docs as a labeling JSONL queue.

    Stream A continuation: the 555 disagreement docs in the corpus are
    where ground-truth labels grow fastest. This CLI exports them as a
    JSONL queue where each line carries side-by-side classifier votes,
    layout signals, a 2000-char text sample, and a suggested label
    (majority vote where one exists).

    Workflow:
      1. doc-intel triage-disagreements --out triage_v0.jsonl
      2. open in a notebook / Streamlit UI, hand-confirm or fix labels
      3. write back to document_type_gold via a follow-up insert
         (use source='human_review', labeler='human:<your-id>')

    Pass --label COVER_LETTER --label RIDER (etc.) to focus the queue on
    specific types currently underrepresented in gold.
    """
    import sqlite3
    from collections import Counter, defaultdict

    settings, _ = _bootstrap()
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Current gold distribution → underrepresented-bucket weights
    c.execute("""
        SELECT label, COUNT(*) AS n FROM document_type_gold
        WHERE superseded_by IS NULL GROUP BY label
    """)
    gold_counts: dict[str, int] = {r["label"]: r["n"] for r in c.fetchall()}
    # Inverse-frequency weight per label — higher weight = bigger gold gap.
    # Labels not in gold get the highest weight (infinity-ish via large constant).
    all_known_labels = [
        "TARIFF_SHEET", "RIDER", "RATE_SCHEDULE", "ORDER_FINAL", "ORDER_PROCEDURAL",
        "TESTIMONY", "COVER_LETTER", "CERTIFICATE_OF_SERVICE", "NOTICE_OF_HEARING",
        "APPLICATION", "COMPLIANCE_FILING", "FERC_ORDER", "EIA_REPORT",
    ]
    label_weights = {}
    for lab in all_known_labels:
        n = gold_counts.get(lab, 0)
        label_weights[lab] = 100.0 / (n + 1)  # n=0 -> weight 100, n=176 -> ~0.6

    # Pull all document_type classifications for state, joined with hd
    c.execute(
        """
        SELECT dc.subject_id AS hd_id_str,
               dc.classifier, dc.label, dc.confidence,
               hd.family_key, hd.title, hd.local_path, hd.start_page, hd.end_page
        FROM document_classifications dc
        JOIN historical_documents hd
          ON CAST(hd.id AS TEXT) = dc.subject_id
         AND dc.subject_kind = 'historical_document'
        WHERE dc.stage = 'document_type' AND hd.state = ?
        """,
        (state,),
    )

    by_doc: dict[str, dict] = defaultdict(lambda: {"votes": [], "meta": None})
    for r in c.fetchall():
        if by_doc[r["hd_id_str"]]["meta"] is None:
            by_doc[r["hd_id_str"]]["meta"] = {
                "family_key": r["family_key"],
                "title": r["title"],
                "local_path": r["local_path"],
                "start_page": r["start_page"],
                "end_page": r["end_page"],
            }
        by_doc[r["hd_id_str"]]["votes"].append({
            "classifier": r["classifier"],
            "label": r["label"],
            "confidence": round(r["confidence"], 3),
        })

    # Skip docs that already have an active gold row — they're settled.
    c.execute(
        """SELECT subject_id FROM document_type_gold
           WHERE subject_kind='historical_document' AND superseded_by IS NULL"""
    )
    settled = {r["subject_id"] for r in c.fetchall()}

    label_filter_set = {lab.upper() for lab in (label_filter or [])}

    candidates: list[dict] = []
    for hd_id_str, payload in by_doc.items():
        if hd_id_str in settled:
            continue
        votes = payload["votes"]
        labels = [v["label"] for v in votes]
        if len(set(labels)) < 2 or len(votes) < 2:
            # Not a disagreement (either too few classifiers or unanimous)
            continue
        if label_filter_set and not (label_filter_set & set(labels)):
            continue

        # Priority score: average of underrepresented-bucket weights across
        # votes. Average (not sum) so a doc with all-rare-label votes ranks
        # above a doc with one rare + several common votes — the all-rare
        # doc is more diagnostic for gold-set growth in those buckets.
        priority = sum(label_weights.get(lab, 1.0) for lab in labels) / max(1, len(labels))
        if weight_underrepresented is False:
            priority = 1.0

        candidates.append({
            "hd_id": int(hd_id_str),
            "priority": round(priority, 2),
            "votes": votes,
            "labels_voted": sorted(set(labels)),
            "majority_label": Counter(labels).most_common(1)[0][0],
            **payload["meta"],
        })

    # Sort by priority (high first), then hd_id for stability
    candidates.sort(key=lambda c: (-c["priority"], c["hd_id"]))
    candidates = candidates[:limit]

    # Enrich with text sample (last because slow)
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import (
        BulkExtractor, normalize_docling_markdown, normalize_ocr_text,
    )
    extractor = BulkExtractor(db_path=str(settings.database_path))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with output_path.open("w", encoding="utf-8") as f:
        for cand in candidates:
            doc = extractor.get_document_for_extraction(cand["hd_id"])
            text_sample = ""
            text_source = "none"
            if doc:
                try:
                    text, src = extractor.extract_text_from_pdf(
                        doc["local_path"],
                        start_page=doc.get("start_page"),
                        end_page=doc.get("end_page"),
                    )
                    if src in ("docling_artifact", "docling_artifact_sliced"):
                        text = normalize_docling_markdown(text)
                    text_sample = normalize_ocr_text(text)[:2000]
                    text_source = src
                except Exception:
                    pass
            row = {
                **cand,
                "text_sample": text_sample,
                "text_source": text_source,
            }
            # Remove non-JSON fields
            row.pop("local_path", None)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1

    conn.close()

    typer.echo(f"\nTriage queue exported | state={state}")
    typer.echo(f"  candidates considered:   {len(by_doc)}")
    typer.echo(f"  disagreement docs:       {len(candidates) if not limit else 'capped at limit'}")
    typer.echo(f"  written to {output_path}: {written}")
    if weight_underrepresented:
        typer.echo("\n  Underrepresented-bucket label weights (gold counts in parens):")
        for lab, w in sorted(label_weights.items(), key=lambda kv: -kv[1]):
            n = gold_counts.get(lab, 0)
            typer.echo(f"    {lab:<28} weight={w:.2f}  gold_n={n}")


@doc_intel_app.command("classify-documents-v2")
def classify_documents_v2_nc(
    state: str = typer.Option("NC", "--state", help="State filter for historical_documents."),
    limit: int | None = typer.Option(None, "--limit", help="Limit docs scored (default: all)."),
    write_classifications: bool = typer.Option(
        False,
        "--write-classifications",
        help=(
            "Persist v2 classifications to document_classifications. Without "
            "this flag the command only prints a comparison report against "
            "v1 (rule_document_type_v1)."
        ),
    ),
    show_disagreements: int = typer.Option(
        15,
        "--show-disagreements",
        help="Show up to N v1 vs v2 disagreement examples.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit summary as JSON."),
) -> None:
    """Run rule_document_type_v2 against NC docs and compare with v1.

    Pulls a DocumentSignals snapshot for each doc (title, first 2k chars,
    last 1k chars, layout features from document_fingerprints when
    available), runs the new per-type classifier, and reports:

      - confidence distribution (avg/min/max, hi/lo bands)
      - label distribution
      - per-doc disagreements with the v1 classifier
      - optional persistence to document_classifications

    Part of Stream B in docs/research/document_identification.md.
    """
    import sqlite3
    from collections import Counter
    from duke_rates.classification.rule_document_type_v2 import (
        DocumentSignals,
        classify_v2,
        CLASSIFIER_NAME as V2_NAME,
        CLASSIFIER_VERSION as V2_VERSION,
    )
    from duke_rates.historical.ncuc.pipeline.bulk_extractor import (
        BulkExtractor, normalize_docling_markdown, normalize_ocr_text,
    )

    settings, _ = _bootstrap()
    extractor = BulkExtractor(db_path=str(settings.database_path))
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row

    sql = """
        SELECT hd.id, hd.title, hd.family_key
        FROM historical_documents hd
        WHERE hd.state = ?
        ORDER BY hd.id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, (state,)).fetchall()
    typer.echo(f"Scoring {len(rows)} docs with rule_document_type_v2...")

    # Fingerprint lookup for layout signals
    fp_by_pdf: dict[str, sqlite3.Row] = {}
    for fp in conn.execute(
        "SELECT source_pdf, page_count, text_chars, has_tables FROM document_fingerprints_v2"
    ).fetchall():
        fp_by_pdf[fp["source_pdf"]] = fp

    # v1 label lookup for comparison
    v1_by_hd: dict[int, str] = {}
    for r in conn.execute(
        """SELECT subject_id, label FROM document_classifications
           WHERE classifier='rule_document_type_v1' AND stage='document_type'"""
    ).fetchall():
        try:
            v1_by_hd[int(r["subject_id"])] = r["label"]
        except (TypeError, ValueError):
            continue

    results: list[dict] = []
    label_counts: Counter = Counter()
    confidence_buckets = {"high": 0, "mid": 0, "low": 0}
    disagreements: list[dict] = []

    for r in rows:
        hd_id = int(r["id"])
        doc = extractor.get_document_for_extraction(hd_id)
        if not doc:
            continue
        try:
            text, src = extractor.extract_text_from_pdf(
                doc["local_path"],
                start_page=doc.get("start_page"),
                end_page=doc.get("end_page"),
            )
            if src in ("docling_artifact", "docling_artifact_sliced"):
                text = normalize_docling_markdown(text)
            text = normalize_ocr_text(text)
        except Exception:
            text = ""
        if not text:
            continue
        first_text = text[:2000]
        last_text = text[-1000:] if len(text) > 2000 else ""

        fp = fp_by_pdf.get(doc["local_path"])
        signals = DocumentSignals(
            title=r["title"] or "",
            first_text=first_text,
            last_text=last_text,
            page_count=fp["page_count"] if fp else None,
            text_chars=fp["text_chars"] if fp else len(text),
            has_tables=fp["has_tables"] if fp else None,
        )
        result = classify_v2(signals)
        label_counts[result.label] += 1
        if result.confidence >= 0.9:
            confidence_buckets["high"] += 1
        elif result.confidence >= 0.5:
            confidence_buckets["mid"] += 1
        else:
            confidence_buckets["low"] += 1

        v1_label = v1_by_hd.get(hd_id)
        if v1_label and v1_label != result.label:
            disagreements.append({
                "hd_id": hd_id,
                "v1_label": v1_label,
                "v2_label": result.label,
                "v2_confidence": round(result.confidence, 3),
                "title": (r["title"] or "")[:60],
            })

        if write_classifications:
            from duke_rates.classification.persistence import record_classification
            try:
                record_classification(
                    conn,
                    subject_kind="historical_document",
                    subject_id=str(hd_id),
                    stage="document_type",
                    result=result,
                )
            except Exception as exc:
                logger.debug(f"v2 persist failed for hd={hd_id}: {exc}")

        results.append({
            "hd_id": hd_id, "label": result.label, "confidence": result.confidence,
        })

    if write_classifications:
        conn.commit()

    summary = {
        "state": state,
        "docs_scored": len(results),
        "confidence_buckets": confidence_buckets,
        "label_distribution": dict(label_counts.most_common()),
        "disagreements_total": len(disagreements),
    }

    if json_out:
        typer.echo(json.dumps(summary, indent=2))
        conn.close()
        return

    typer.echo(f"\nrule_document_type_v2 scoring | state={state}")
    typer.echo(f"  docs scored:                {summary['docs_scored']}")
    typer.echo(f"  high-confidence (>=0.9):    {confidence_buckets['high']}")
    typer.echo(f"  mid-confidence (0.5-0.9):   {confidence_buckets['mid']}")
    typer.echo(f"  low-confidence (<0.5):      {confidence_buckets['low']}")
    typer.echo("\n  Label distribution (v2):")
    for label, n in label_counts.most_common():
        typer.echo(f"    {label:<28} {n}")

    typer.echo(f"\n  v1 vs v2 disagreements: {len(disagreements)}")
    if disagreements and show_disagreements:
        for d in disagreements[:show_disagreements]:
            typer.echo(
                f"    hd={d['hd_id']:<5} v1={d['v1_label']:<20} -> v2={d['v2_label']:<22} "
                f"conf={d['v2_confidence']:.2f}  {d['title']!r}"
            )

    if write_classifications:
        typer.echo(f"\n  Wrote {len(results)} v2 classifications to document_classifications.")
    else:
        typer.echo(
            "\n  Dry-run only. Pass --write-classifications to persist v2 results."
        )

    conn.close()


@doc_intel_app.command("promote-sections-to-gold")
def promote_sections_to_gold_cmd(
    section_types: str = typer.Option(
        "rate_schedule,rider,terms_conditions,cover_letter,procedural",
        "--section-types",
        help=(
            "Comma-separated section types to consider. The 'unknown' type "
            "is never promoted regardless of inclusion."
        ),
    ),
    min_classifiers: int = typer.Option(
        2,
        "--min-classifiers",
        help=(
            "Minimum number of classifiers that must agree on a section's "
            "type before it is eligible for gold. section_aggregator_v1 "
            "always counts as one — so 2 means at least one doc-level "
            "classifier (rule, embedding KNN, or LLM) also has to map to "
            "the same section type."
        ),
    ),
    min_confidence_override: float | None = typer.Option(
        None,
        "--min-confidence",
        help=(
            "Override the per-type confidence floors with a single global "
            "threshold. Useful for one-off experiments; leave unset for "
            "production runs (rate_schedule=0.75, rider=0.70, "
            "procedural=0.45, etc.)."
        ),
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        help="Cap the number of candidate sections evaluated.",
    ),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Actually write gold rows. Without this flag, dry-run only.",
    ),
    promoted_by: str | None = typer.Option(
        None,
        "--promoted-by",
        help="Free-text label for who/what triggered this run (e.g. 'cli', "
        "'overnight_loop_cycle_5'). Stored on each new gold row.",
    ),
    no_log_conflicts: bool = typer.Option(
        False,
        "--no-log-conflicts",
        help="Skip writing rejected-as-conflict candidates to "
        "section_classification_conflicts. Default: log them.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit summary as JSON."),
) -> None:
    """Promote high-confidence section_aggregator outputs to section_type_gold.

    Section-level training corpus seeder. Each section in document_sections
    that clears (a) a per-type confidence floor, (b) doc-level classifier
    agreement, and (c) consistency with the parent document's classifier
    consensus gets a row in section_type_gold.

    Rejected-as-conflict sections (where section_type contradicts doc-level
    consensus) are logged to section_classification_conflicts for triage —
    these are exactly the cases LLM adjudication should focus on next.

    Idempotent. Re-promoting a section with a new label supersedes the
    old row (sets superseded_by). Re-running with no label changes is a
    no-op.

    Examples:

      # Dry-run sanity check (default)
      doc-intel promote-sections-to-gold

      # Real promotion for rate sections only
      doc-intel promote-sections-to-gold --execute --section-types rate_schedule,rider

      # Aggressive: single 0.5 floor across all types
      doc-intel promote-sections-to-gold --execute --min-confidence 0.5
    """
    from duke_rates.document_intelligence.section_gold_promotion import (
        promote_sections,
    )

    settings, _ = _bootstrap()

    types_list = [t.strip() for t in section_types.split(",") if t.strip()]
    if not types_list:
        raise typer.BadParameter("--section-types must list at least one type")

    run = promote_sections(
        settings.database_path,
        section_types=types_list,
        min_classifiers_agreed=min_classifiers,
        min_section_confidence_override=min_confidence_override,
        limit=limit,
        dry_run=not execute,
        gold_source="auto_promotion",
        promoted_by=promoted_by or ("cli_execute" if execute else "cli_dry_run"),
        log_conflicts=not no_log_conflicts,
    )

    if json_out:
        # Sample lists may contain dataclasses; serialize defensively
        def _ser(x):
            if hasattr(x, "__dict__"):
                return {k: v for k, v in x.__dict__.items()}
            return x
        payload = {
            "mode": "execute" if execute else "dry_run",
            "section_types": types_list,
            "candidates_evaluated": run.candidates_evaluated,
            "promoted": run.promoted,
            "skipped_already_gold": run.skipped_already_gold,
            "skipped_low_confidence": run.skipped_low_confidence,
            "skipped_no_consensus": run.skipped_no_consensus,
            "rejected_conflict": run.rejected_conflict,
            "rejected_other": run.rejected_other,
            "by_type": run.by_type,
            "sample_promotions": [_ser(p) for p in run.sample_promotions],
            "sample_conflicts": [_ser(p) for p in run.sample_conflicts],
        }
        typer.echo(json.dumps(payload, indent=2, default=str))
        return

    mode = "EXECUTE" if execute else "DRY-RUN"
    typer.echo(f"=== Section gold promotion ({mode}) ===")
    typer.echo(f"  section_types:       {','.join(types_list)}")
    typer.echo(f"  min_classifiers:     {min_classifiers}")
    if min_confidence_override is not None:
        typer.echo(f"  confidence_override: {min_confidence_override}")
    if limit:
        typer.echo(f"  limit:               {limit}")
    typer.echo("")
    # Effective-count line in canonical format for the autonomous loop's
    # M1 stdout parser. The pattern "promoted N" doesn't currently match
    # any pattern in _EFFECTIVE_COUNT_PATTERNS but matches the spirit of
    # "inserted=N". Use "Done: created=N skipped=M" so the existing
    # bootstrap pattern catches it.
    typer.echo(
        f"Done: created={run.promoted} skipped="
        f"{run.skipped_already_gold + run.skipped_low_confidence + run.skipped_no_consensus + run.rejected_other}"
    )
    typer.echo(f"  candidates_evaluated:   {run.candidates_evaluated}")
    typer.echo(f"  promoted:               {run.promoted}")
    typer.echo(f"  skipped_already_gold:   {run.skipped_already_gold}")
    typer.echo(f"  skipped_low_confidence: {run.skipped_low_confidence}")
    typer.echo(f"  skipped_no_consensus:   {run.skipped_no_consensus}")
    typer.echo(f"  rejected_conflict:      {run.rejected_conflict}")
    typer.echo(f"  rejected_other:         {run.rejected_other}")

    if run.by_type:
        typer.echo("")
        typer.echo("  promoted by type:")
        for t, n in sorted(run.by_type.items(), key=lambda kv: (-kv[1], kv[0])):
            typer.echo(f"    {t:25s} {n}")

    if run.sample_promotions:
        typer.echo("")
        typer.echo("  sample promotions (first 5):")
        for p in run.sample_promotions:
            code = p.schedule_code or p.rider_code or "-"
            typer.echo(
                f"    {p.source_pdf[-50:]:50s} idx={p.section_index:>3} "
                f"type={p.section_type:18s} code={code:12s} "
                f"conf={p.confidence:.2f} n_clf={p.n_classifiers_agreed}"
            )

    if run.sample_conflicts:
        typer.echo("")
        typer.echo("  sample conflicts (first 5 — review or adjudicate):")
        for p in run.sample_conflicts:
            typer.echo(
                f"    {p.source_pdf[-50:]:50s} idx={p.section_index:>3} "
                f"section_says={p.section_type:18s} reason={(p.reject_reason or '')[:60]}"
            )

    if not execute:
        typer.echo("")
        typer.echo("  Dry-run only. Pass --execute to write rows.")


@doc_intel_app.command("benchmark-knn-label-source")
def benchmark_knn_label_source(
    json_output: bool = typer.Option(
        False, "--json", help="Emit a JSON report instead of a human summary."
    ),
) -> None:
    """Compare neighbor-label sources for embedding_knn_v1 (no embedding needed).

    For every PDF that has both section_type_gold and a rule_v1 classification,
    compute (a) the doc_type derived from section gold, (b) the rule_v1 label,
    and report agreement and the most common disagreement patterns. This is the
    upstream signal driving the KNN improvement — if these labels agree on a
    given neighbor, the KNN vote is unchanged. Where they disagree, the
    'section_gold_or_rule' mode will vote with the section-derived label.
    """
    import sqlite3
    from collections import Counter

    from duke_rates.document_intelligence.section_derived_labels import (
        derive_doc_type_from_sections,
    )

    settings, _ = _bootstrap()

    conn = sqlite3.connect(str(settings.database_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT s.source_pdf,
                   GROUP_CONCAT(DISTINCT s.section_type) AS section_types,
                   dc.label AS rule_label,
                   dc.confidence AS rule_conf
            FROM section_type_gold s
            JOIN historical_documents hd ON hd.local_path = s.source_pdf
            JOIN document_classifications dc
              ON dc.subject_kind = 'historical_document'
             AND dc.subject_id = CAST(hd.id AS TEXT)
             AND dc.stage = 'document_type'
             AND dc.classifier = 'rule_document_type_v1'
             AND dc.superseded_by IS NULL
            WHERE s.superseded_by IS NULL
            GROUP BY s.source_pdf, dc.label, dc.confidence
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        typer.echo("No PDFs with both section gold and rule_v1 labels.")
        raise typer.Exit(code=0)

    total = len(rows)
    agreements = 0
    disagreements: Counter[tuple[str, str]] = Counter()
    none_derived = 0
    for row in rows:
        section_types = set(row["section_types"].split(","))
        derived = derive_doc_type_from_sections(section_types)
        rule = row["rule_label"]
        if derived is None:
            none_derived += 1
            continue
        if derived == rule:
            agreements += 1
        else:
            disagreements[(derived, rule)] += 1

    if json_output:
        report = {
            "total_comparable_pdfs": total,
            "agreement": agreements,
            "agreement_pct": round(100 * agreements / total, 2),
            "disagreement": sum(disagreements.values()),
            "no_derivation_possible": none_derived,
            "top_disagreements": [
                {"derived": d, "rule_v1": r, "count": n}
                for (d, r), n in disagreements.most_common(15)
            ],
        }
        typer.echo(json.dumps(report, indent=2))
        return

    typer.echo("=== KNN label-source benchmark ===")
    typer.echo(f"  comparable PDFs:       {total}")
    typer.echo(
        f"  agreement:             {agreements} "
        f"({100 * agreements / total:.1f}%)"
    )
    typer.echo(
        f"  disagreement:          {sum(disagreements.values())} "
        f"({100 * sum(disagreements.values()) / total:.1f}%)"
    )
    typer.echo(f"  no derivation:         {none_derived}")
    typer.echo("")
    typer.echo("  Top disagreements (derived vs rule_v1):")
    for (derived, rule), n in disagreements.most_common(15):
        typer.echo(
            f"    {n:4d}  derived={derived:<22} rule_v1={rule:<22}"
        )
    typer.echo("")
    typer.echo(
        "  Where these disagree, 'section_gold_or_rule' label_source will vote with"
    )
    typer.echo("  the derived label (higher-quality signal from human-curated gold).")


@doc_intel_app.command("embed-sections")
def embed_sections_nc(
    limit: int = typer.Option(0, "--limit", help="Only embed N sections (0 = all)."),
    skip_existing: bool = typer.Option(
        True,
        "--skip-existing/--no-skip-existing",
        help="Skip sections already in section_embeddings for the active model+kind.",
    ),
    embedding_kind: str = typer.Option(
        "section_text", "--embedding-kind", help="Embedding slice key."
    ),
    model_role: str = typer.Option(
        "embedding_primary",
        "--model-role",
        help=(
            "OllamaOrchestrator role to embed with. Use 'embedding_secondary' "
            "(bge-m3) to populate a second axis for retriever comparison."
        ),
    ),
    max_chars: int = typer.Option(
        2000, "--max-chars", help="Truncate section text to this many chars."
    ),
    progress: bool = typer.Option(True, "--progress/--no-progress"),
) -> None:
    """Embed per-section text from document_sections into section_embeddings.

    For each section, text is built by concatenating ncuc_page_artifacts
    rows in the section's start_page..end_page range. The active embedding
    model (orchestrator role 'embedding_primary') is used. Writes are
    idempotent on (source_pdf, section_index, embedding_kind, model, version).
    """
    import struct

    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.ollama_orchestrator import (
        OllamaOrchestrator,
    )
    from duke_rates.document_intelligence.section_text_extractor import (
        fetch_section_text,
    )

    settings, _ = _bootstrap()

    orch = OllamaOrchestrator()
    if model_role not in orch._roles:
        typer.echo(
            f"Unknown model_role {model_role!r}. "
            f"Available: {sorted(orch._roles.keys())}",
            err=True,
        )
        raise typer.Exit(code=1)
    model = orch._roles[model_role].primary

    conn = connect(settings.database_path)
    try:
        if skip_existing:
            rows = conn.execute(
                """
                SELECT s.id, s.source_pdf, s.section_index, s.start_page, s.end_page
                FROM document_sections s
                WHERE NOT EXISTS (
                    SELECT 1 FROM section_embeddings e
                    WHERE e.source_pdf = s.source_pdf
                      AND e.section_index = s.section_index
                      AND e.embedding_kind = ?
                      AND e.embedding_model = ?
                )
                ORDER BY s.source_pdf, s.section_index
                """,
                (embedding_kind, model),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, source_pdf, section_index, start_page, end_page
                FROM document_sections
                ORDER BY source_pdf, section_index
                """
            ).fetchall()
    finally:
        conn.close()

    sections = [dict(r) for r in rows]
    if limit > 0:
        sections = sections[:limit]

    if not sections:
        typer.echo(
            f"No sections to embed (kind={embedding_kind}, model={model})."
        )
        return

    typer.echo(
        f"Embedding {len(sections)} sections (kind={embedding_kind}, model={model})..."
    )

    ok = skip = fail = 0
    for i, sec in enumerate(sections):
        conn = connect(settings.database_path)
        try:
            sec_text = fetch_section_text(
                conn,
                sec["source_pdf"],
                int(sec["start_page"]),
                int(sec["end_page"]),
                max_chars=max_chars,
            )
        finally:
            conn.close()

        if not sec_text.text.strip():
            skip += 1
            continue

        try:
            vector = orch.embed(model_role, sec_text.text)
        except Exception:
            fail += 1
            continue

        blob = struct.pack(f"{len(vector)}f", *vector)
        conn = connect(settings.database_path)
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO section_embeddings
                    (source_pdf, section_index, start_page, end_page,
                     embedding_kind, embedding_model, embedding_version,
                     vector, text_sample)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    sec["source_pdf"],
                    int(sec["section_index"]),
                    int(sec["start_page"]),
                    int(sec["end_page"]),
                    embedding_kind,
                    model,
                    "v1",
                    blob,
                    sec_text.text[:200],
                ),
            )
            conn.commit()
            ok += 1
        except Exception:
            fail += 1
        finally:
            conn.close()

        if progress and (i + 1) % 50 == 0:
            typer.echo(
                f"  {i + 1}/{len(sections)} ok={ok} skip={skip} fail={fail}",
                err=True,
            )

    typer.echo(f"Done: ok={ok} skip={skip} fail={fail}")


@doc_intel_app.command("classify-sections")
def classify_sections_nc(
    limit: int = typer.Option(0, "--limit", help="Only classify N sections (0 = all)."),
    skip_existing: bool = typer.Option(
        True,
        "--skip-existing/--no-skip-existing",
        help="Skip sections that already have an active section_knn_v1 row.",
    ),
    skip_gold: bool = typer.Option(
        True,
        "--skip-gold/--no-skip-gold",
        help="Skip sections that are already in section_type_gold.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    progress: bool = typer.Option(True, "--progress/--no-progress"),
    k: int = typer.Option(9, "--k"),
    min_neighbors: int = typer.Option(3, "--min-neighbors"),
) -> None:
    """Classify sections via SectionKNNClassifier and persist to document_classifications.

    Subject keying: ``subject_kind='document_section'``,
    ``subject_id=str(document_sections.id)``, ``stage='section_type'``,
    ``classifier='section_knn_v1'``. Each section's classification can be
    used to propose new section_type_gold rows after adjudication.
    """
    from duke_rates.classification.persistence import record_classification
    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.ollama_orchestrator import (
        OllamaOrchestrator,
    )
    from duke_rates.document_intelligence.section_classifier import (
        SectionKNNClassifier,
    )
    from duke_rates.document_intelligence.section_text_extractor import (
        fetch_section_text,
    )

    settings, _ = _bootstrap()

    where_clauses = ["1=1"]
    if skip_existing:
        where_clauses.append(
            """NOT EXISTS (
                SELECT 1 FROM document_classifications dc
                WHERE dc.subject_kind = 'document_section'
                  AND dc.subject_id = CAST(s.id AS TEXT)
                  AND dc.stage = 'section_type'
                  AND dc.classifier = 'section_knn_v1'
                  AND dc.superseded_by IS NULL
            )"""
        )
    if skip_gold:
        where_clauses.append(
            """NOT EXISTS (
                SELECT 1 FROM section_type_gold g
                WHERE g.source_pdf = s.source_pdf
                  AND g.section_index = s.section_index
                  AND g.superseded_by IS NULL
            )"""
        )

    where_sql = " AND ".join(where_clauses)

    conn = connect(settings.database_path)
    try:
        rows = conn.execute(
            f"""
            SELECT s.id, s.source_pdf, s.section_index, s.start_page, s.end_page
            FROM document_sections s
            WHERE {where_sql}
            ORDER BY s.source_pdf, s.section_index
            """
        ).fetchall()
        emb_count = conn.execute(
            "SELECT COUNT(*) FROM section_embeddings"
        ).fetchone()[0]
        gold_count = conn.execute(
            "SELECT COUNT(*) FROM section_type_gold WHERE superseded_by IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()

    if emb_count == 0:
        typer.echo(
            "No section_embeddings — run doc-intel embed-sections first.",
            err=True,
        )
        raise typer.Exit(code=1)
    if gold_count == 0:
        typer.echo(
            "No section_type_gold rows — KNN cannot vote. Promote some "
            "sections first via doc-intel promote-sections-to-gold.",
            err=True,
        )
        raise typer.Exit(code=1)

    sections = [dict(r) for r in rows]
    if limit > 0:
        sections = sections[:limit]
    if not sections:
        typer.echo("No sections to classify.")
        return

    typer.echo(
        f"Classifying {len(sections)} sections (ref embeddings={emb_count}, "
        f"gold neighbors={gold_count})..."
    )
    if dry_run:
        typer.echo("[DRY RUN — no rows will be written]")

    orch = OllamaOrchestrator()
    clf = SectionKNNClassifier(
        db_path=settings.database_path,
        orchestrator=orch,
        k=k,
        min_neighbors=min_neighbors,
    )

    ok = skip = fail = 0
    label_dist: dict[str, int] = {}
    for i, sec in enumerate(sections):
        conn = connect(settings.database_path)
        try:
            sec_text = fetch_section_text(
                conn,
                sec["source_pdf"],
                int(sec["start_page"]),
                int(sec["end_page"]),
                max_chars=2000,
            )
        finally:
            conn.close()

        if not sec_text.text.strip():
            skip += 1
            continue

        try:
            result = clf.classify(
                sec_text.text,
                exclude_key=(sec["source_pdf"], int(sec["section_index"])),
            )
        except Exception:
            fail += 1
            continue

        if result.label == "unknown":
            skip += 1
            continue

        label_dist[result.label] = label_dist.get(result.label, 0) + 1

        if not dry_run:
            cls_conn = connect(settings.database_path)
            try:
                record_classification(
                    cls_conn,
                    subject_kind="document_section",
                    subject_id=str(sec["id"]),
                    stage="section_type",
                    result=result,
                )
                cls_conn.commit()
            finally:
                cls_conn.close()
        ok += 1

        if progress and (i + 1) % 50 == 0:
            typer.echo(
                f"  {i + 1}/{len(sections)} ok={ok} skip={skip} fail={fail}",
                err=True,
            )

    typer.echo(f"\nDone: ok={ok} skip={skip} fail={fail}")
    if label_dist:
        typer.echo("  predicted label distribution:")
        for lbl, n in sorted(label_dist.items(), key=lambda x: -x[1]):
            typer.echo(f"    {lbl:20s} {n}")


@doc_intel_app.command("rag-search")
def rag_search(
    query: str = typer.Argument(..., help="Natural-language question or keyword phrase."),
    top_k: int = typer.Option(10, "--top-k", "-k", help="Number of results to return."),
    section_types: str = typer.Option(
        "",
        "--section-types",
        help="Comma-separated section_type filter (e.g. rate_schedule,rider).",
    ),
    schedule_code: str = typer.Option(
        "",
        "--schedule-code",
        help="Substring match on schedule_codes (case-insensitive).",
    ),
    source_pdf: str = typer.Option(
        "",
        "--source-pdf",
        help="Substring match on source_pdf path (case-insensitive).",
    ),
    min_similarity: float = typer.Option(
        0.0,
        "--min-similarity",
        help="Drop hits below this cosine similarity.",
    ),
    excerpt_chars: int = typer.Option(
        400,
        "--excerpt-chars",
        help="Chars of section text to include in each hit.",
    ),
    model_role: str = typer.Option(
        "embedding_primary",
        "--model-role",
        help="Orchestrator role used to embed the query AND select reference vectors.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit JSON instead of human-readable table."
    ),
) -> None:
    """Section-level RAG retriever (R1) — no generation, just retrieval.

    Embeds the query, runs cosine similarity against section_embeddings,
    applies optional metadata filters, and prints the top-k matches with
    citation-grade metadata and a text excerpt.
    """
    import json as _json

    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.ollama_orchestrator import (
        OllamaOrchestrator,
    )
    from duke_rates.document_intelligence.rag_retriever import RagRetriever

    settings, _ = _bootstrap()

    # Light corpus sanity check
    conn = connect(settings.database_path)
    try:
        n_emb = conn.execute(
            "SELECT COUNT(*) FROM section_embeddings"
        ).fetchone()[0]
    finally:
        conn.close()
    if n_emb == 0:
        typer.echo(
            "section_embeddings is empty — run `doc-intel embed-sections` first.",
            err=True,
        )
        raise typer.Exit(code=1)

    orch = OllamaOrchestrator()
    if model_role not in orch._roles:
        typer.echo(
            f"Unknown model_role {model_role!r}. Available: {sorted(orch._roles.keys())}",
            err=True,
        )
        raise typer.Exit(code=1)
    retriever = RagRetriever(
        db_path=settings.database_path,
        orchestrator=orch,
        model_role=model_role,
        excerpt_chars=excerpt_chars,
    )

    type_list = (
        [t.strip() for t in section_types.split(",") if t.strip()]
        if section_types
        else None
    )

    hits = retriever.search(
        query,
        top_k=top_k,
        section_types=type_list,
        schedule_code_like=schedule_code or None,
        source_pdf_like=source_pdf or None,
        min_similarity=min_similarity,
    )

    if json_output:
        out = [
            {
                "rank": i + 1,
                "citation": h.citation(),
                "source_pdf": h.source_pdf,
                "section_index": h.section_index,
                "start_page": h.start_page,
                "end_page": h.end_page,
                "similarity": round(h.similarity, 4),
                "section_type": h.section_type,
                "section_type_source": h.section_type_source,
                "section_type_conf": (
                    round(h.section_type_conf, 4)
                    if h.section_type_conf is not None
                    else None
                ),
                "schedule_codes": h.schedule_codes,
                "rider_codes": h.rider_codes,
                "leaf_numbers": h.leaf_numbers,
                "text_excerpt": h.text_excerpt,
            }
            for i, h in enumerate(hits)
        ]
        typer.echo(_json.dumps(out, indent=2))
        return

    typer.echo(f"\n=== RAG search: {query!r} ===")
    typer.echo(
        f"  filters: section_types={type_list or 'any'} "
        f"schedule~{schedule_code or '*'} pdf~{source_pdf or '*'} "
        f"min_sim={min_similarity}"
    )
    typer.echo(f"  reference corpus: {n_emb} section embeddings")
    typer.echo(f"  returned: {len(hits)} hits\n")

    if not hits:
        typer.echo("  No matches.")
        return

    for i, h in enumerate(hits):
        conf_str = (
            f" conf={h.section_type_conf:.2f}" if h.section_type_conf else ""
        )
        sec_str = f"{h.section_type or '?'}({h.section_type_source}){conf_str}"
        codes: list[str] = []
        if h.schedule_codes:
            codes.append(f"sched={','.join(h.schedule_codes[:3])}")
        if h.rider_codes:
            codes.append(f"rider={','.join(h.rider_codes[:3])}")
        if h.leaf_numbers:
            codes.append(f"leaf={','.join(h.leaf_numbers[:3])}")
        codes_str = "  ".join(codes) if codes else ""
        excerpt = h.text_excerpt.replace("\n", " ").replace("\f", "|")[:300]
        typer.echo(
            f"  [{i + 1}] sim={h.similarity:.3f}  {h.citation()}"
        )
        typer.echo(f"      {sec_str}  {codes_str}")
        typer.echo(f"      {excerpt}")
        typer.echo("")


@doc_intel_app.command("rag-answer")
def rag_answer(
    question: str = typer.Argument(..., help="The question to answer."),
    top_k: int = typer.Option(8, "--top-k", "-k", help="Sections to retrieve."),
    section_types: str = typer.Option(
        "", "--section-types", help="Comma-separated section_type filter."
    ),
    schedule_code: str = typer.Option(
        "", "--schedule-code", help="Substring match on schedule_codes."
    ),
    source_pdf: str = typer.Option(
        "", "--source-pdf", help="Substring match on source_pdf path."
    ),
    min_similarity: float = typer.Option(0.0, "--min-similarity"),
    embedding_role: str = typer.Option(
        "embedding_primary",
        "--embedding-role",
        help="Orchestrator role for query embedding + reference vector pool.",
    ),
    generation_role: str = typer.Option(
        "balanced_classifier",
        "--generation-role",
        help="Ollama orchestrator role for synthesis (default: qwen3:8b).",
    ),
    max_context_chars: int = typer.Option(
        8000, "--max-context-chars", help="Cap on total context block bytes."
    ),
    max_excerpt_chars: int = typer.Option(
        800, "--max-excerpt-chars", help="Cap on each excerpt."
    ),
    show_uncited: bool = typer.Option(
        False,
        "--show-uncited/--hide-uncited",
        help="Show retrieved sections the LLM did not cite.",
    ),
    show_prompt: bool = typer.Option(
        False, "--show-prompt", help="Print the full prompt sent to the LLM."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit JSON instead of human-readable output."
    ),
) -> None:
    """End-to-end RAG: retrieve sections, then synthesize an answer with citations.

    The LLM is instructed to answer ONLY from the retrieved context and to cite
    every claim with [N]. If the answer is not in the indexed corpus, it must
    say so rather than hallucinate.
    """
    import json as _json

    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.ollama_orchestrator import (
        OllamaOrchestrator,
    )
    from duke_rates.document_intelligence.rag_generator import RagGenerator
    from duke_rates.document_intelligence.rag_retriever import RagRetriever

    settings, _ = _bootstrap()

    conn = connect(settings.database_path)
    try:
        n_emb = conn.execute(
            "SELECT COUNT(*) FROM section_embeddings"
        ).fetchone()[0]
    finally:
        conn.close()
    if n_emb == 0:
        typer.echo(
            "section_embeddings is empty — run `doc-intel embed-sections` first.",
            err=True,
        )
        raise typer.Exit(code=1)

    orch = OllamaOrchestrator()
    if embedding_role not in orch._roles:
        typer.echo(
            f"Unknown --embedding-role {embedding_role!r}. "
            f"Available: {sorted(orch._roles.keys())}",
            err=True,
        )
        raise typer.Exit(code=1)
    retriever = RagRetriever(
        db_path=settings.database_path,
        orchestrator=orch,
        model_role=embedding_role,
        excerpt_chars=max_excerpt_chars,
    )
    gen = RagGenerator(
        retriever=retriever,
        orchestrator=orch,
        generation_role=generation_role,
        top_k=top_k,
        max_context_chars=max_context_chars,
        max_excerpt_chars=max_excerpt_chars,
        include_prompt=show_prompt,
    )

    type_list = (
        [t.strip() for t in section_types.split(",") if t.strip()]
        if section_types
        else None
    )

    answer = gen.answer(
        question,
        section_types=type_list,
        schedule_code_like=schedule_code or None,
        source_pdf_like=source_pdf or None,
        min_similarity=min_similarity,
    )

    if json_output:
        out = {
            "question": answer.question,
            "answer": answer.answer,
            "is_grounded": answer.is_grounded,
            "cited_indices": answer.cited_indices,
            "llm_model": answer.llm_model,
            "llm_status": answer.llm_status,
            "retrieval_ms": round(answer.retrieval_ms, 1),
            "generation_ms": round(answer.generation_ms, 1),
            "cited_hits": [
                {
                    "rank": rank,
                    "citation": h.citation(),
                    "source_pdf": h.source_pdf,
                    "start_page": h.start_page,
                    "end_page": h.end_page,
                    "similarity": round(h.similarity, 4),
                    "section_type": h.section_type,
                    "section_type_source": h.section_type_source,
                }
                for rank, h in answer.cited_hits()
            ],
            "uncited_hits": (
                [
                    {
                        "rank": rank,
                        "citation": h.citation(),
                        "similarity": round(h.similarity, 4),
                    }
                    for rank, h in answer.uncited_hits()
                ]
                if show_uncited
                else None
            ),
            "prompt": answer.prompt if show_prompt else None,
        }
        typer.echo(_json.dumps(out, indent=2))
        return

    typer.echo("")
    typer.echo(f"Q: {answer.question}")
    typer.echo("")
    typer.echo(f"A: {answer.answer}")
    typer.echo("")
    grounded_str = "grounded" if answer.is_grounded else "UNGROUNDED"
    typer.echo(
        f"  [{grounded_str}]  llm={answer.llm_model}({answer.llm_status})  "
        f"retrieval={answer.retrieval_ms:.0f}ms  "
        f"generation={answer.generation_ms:.0f}ms"
    )

    if answer.cited_hits():
        typer.echo("")
        typer.echo("  cited sources:")
        for rank, h in answer.cited_hits():
            typer.echo(f"    [{rank}] sim={h.similarity:.3f}  {h.citation()}")

    if show_uncited and answer.uncited_hits():
        typer.echo("")
        typer.echo("  retrieved but not cited:")
        for rank, h in answer.uncited_hits():
            typer.echo(f"    [{rank}] sim={h.similarity:.3f}  {h.citation()}")

    if show_prompt:
        typer.echo("")
        typer.echo("=" * 60)
        typer.echo("PROMPT:")
        typer.echo(answer.prompt)


@doc_intel_app.command("rag-eval")
def rag_eval(
    eval_set: Path = typer.Option(
        Path("tests/rag_eval_set.yaml"),
        "--eval-set",
        help="Path to a YAML eval set (cases + expected matchers).",
    ),
    full: bool = typer.Option(
        False,
        "--full",
        help="Run generation in addition to retrieval (slow — ~1 min/case).",
    ),
    top_k: int = typer.Option(
        10,
        "--top-k",
        help="Retrieval depth for scoring (and generation context size).",
    ),
    embedding_role: str = typer.Option(
        "embedding_primary",
        "--embedding-role",
        help="Orchestrator role for retrieval embeddings (e.g. embedding_secondary).",
    ),
    generation_role: str = typer.Option(
        "balanced_classifier", "--generation-role"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit a JSON report instead of human summary."
    ),
    case_id: str = typer.Option(
        "", "--case-id", help="Run only one case by id (debug)."
    ),
) -> None:
    """Run the RAG eval harness and print baseline metrics.

    Retrieval-only (default) is fast — seconds per case. Use ``--full`` for
    end-to-end metrics that include LLM generation; this is the regression
    suite for the system but is too slow to run on every change.
    """
    import json as _json

    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.ollama_orchestrator import (
        OllamaOrchestrator,
    )
    from duke_rates.document_intelligence.rag_eval import (
        EvalReport,
        load_eval_set,
        run_full_eval,
        run_retrieval_eval,
    )
    from duke_rates.document_intelligence.rag_generator import RagGenerator
    from duke_rates.document_intelligence.rag_retriever import RagRetriever

    settings, _ = _bootstrap()

    if not eval_set.exists():
        typer.echo(f"Eval set not found: {eval_set}", err=True)
        raise typer.Exit(code=1)
    cases = load_eval_set(eval_set)
    if case_id:
        cases = [c for c in cases if c.id == case_id]
        if not cases:
            typer.echo(f"No case with id={case_id!r}", err=True)
            raise typer.Exit(code=1)

    conn = connect(settings.database_path)
    try:
        n_emb = conn.execute(
            "SELECT COUNT(*) FROM section_embeddings"
        ).fetchone()[0]
    finally:
        conn.close()
    if n_emb == 0:
        typer.echo("section_embeddings is empty — eval cannot run.", err=True)
        raise typer.Exit(code=1)

    orch = OllamaOrchestrator()
    if embedding_role not in orch._roles:
        typer.echo(
            f"Unknown --embedding-role {embedding_role!r}. "
            f"Available: {sorted(orch._roles.keys())}",
            err=True,
        )
        raise typer.Exit(code=1)
    retriever = RagRetriever(
        db_path=settings.database_path,
        orchestrator=orch,
        model_role=embedding_role,
    )

    def _progress(i: int, n: int, cid: str) -> None:
        typer.echo(f"  [{i}/{n}] {cid}", err=True)

    if full:
        gen = RagGenerator(
            retriever=retriever,
            orchestrator=orch,
            generation_role=generation_role,
            top_k=top_k,
        )
        ret_results, gen_results = run_full_eval(
            cases, gen, top_k=top_k, progress_callback=_progress
        )
        report = EvalReport(
            cases=cases,
            retrieval_results=ret_results,
            generation_results=gen_results,
        )
    else:
        ret_results = run_retrieval_eval(
            cases, retriever, top_k=top_k, progress_callback=_progress
        )
        report = EvalReport(cases=cases, retrieval_results=ret_results)

    rm = report.retrieval_metrics()
    gm = report.generation_metrics() if full else None

    if json_output:
        per_case = []
        gen_by_id = (
            {g.case_id: g for g in report.generation_results} if full else {}
        )
        for c, r in zip(report.cases, report.retrieval_results):
            row = {
                "id": c.id,
                "question": c.question,
                "section_types": c.section_types,
                "expected_no_answer": c.expected_no_answer,
                "top1_similarity": r.top1_similarity,
                "expected_rank": r.expected_rank,
                "matched_via": r.matched_via,
            }
            if full and c.id in gen_by_id:
                g = gen_by_id[c.id]
                row.update(
                    {
                        "answer": g.answer_text,
                        "answered": g.answered,
                        "grounded": g.grounded,
                        "keyword_matches": g.keyword_matches,
                        "expected_refusal_correct": g.expected_refusal_correct,
                        "llm_status": g.llm_status,
                        "generation_ms": round(g.generation_ms, 1),
                    }
                )
            per_case.append(row)
        out = {
            "retrieval_metrics": rm,
            "generation_metrics": gm,
            "n_cases": len(cases),
            "per_case": per_case,
        }
        typer.echo(_json.dumps(out, indent=2))
        return

    typer.echo("")
    typer.echo(f"=== RAG eval: {eval_set.name} ===")
    typer.echo(f"  cases:                {len(cases)}")
    typer.echo(f"  retrieval top_k:      {top_k}")
    typer.echo(f"  full (with gen):      {full}")
    typer.echo("")
    typer.echo("Retrieval metrics:")
    typer.echo(f"  cases scored:         {rm['n_cases']}")
    typer.echo(f"  recall@5:             {rm['recall_at_5']}")
    typer.echo(f"  recall@10:            {rm['recall_at_10']}")
    typer.echo(f"  mrr@10:               {rm['mrr_at_10']}")
    typer.echo(f"  avg top-1 similarity: {rm['avg_top1_similarity']}")
    if rm.get("schedule_filter_precision") is not None:
        typer.echo(
            f"  schedule_code filter precision: {rm['schedule_filter_precision']} "
            f"({rm['n_schedule_filtered']} filtered cases)"
        )
    if rm.get("section_type_filter_precision") is not None:
        typer.echo(
            f"  section_type filter precision:  {rm['section_type_filter_precision']} "
            f"({rm['n_type_filtered']} filtered cases)"
        )
    typer.echo("")
    typer.echo("Per-case (rank of first matching hit):")
    typer.echo(f"  {'id':<30} {'rank':<6} {'via':<10} top1_sim")
    for c, r in zip(report.cases, report.retrieval_results):
        if not c.has_retrieval_target:
            continue
        rank_str = str(r.expected_rank) if r.expected_rank else "miss"
        via = r.matched_via or "-"
        sim = (
            f"{r.top1_similarity:.3f}" if r.top1_similarity is not None else "-"
        )
        typer.echo(f"  {c.id:<30} {rank_str:<6} {via:<10} {sim}")

    if full and gm:
        typer.echo("")
        typer.echo("Generation metrics:")
        typer.echo(f"  cases answered:       {gm['n_answered']}/{gm['n_cases']}")
        typer.echo(f"  grounded rate:        {gm['grounded_rate']}")
        typer.echo(f"  keyword match rate:   {gm['keyword_match_rate']}")
        typer.echo(f"  correct refusal rate: {gm['correct_refusal_rate']} "
                   f"({gm['n_refusal_expected']} expected)")


@doc_intel_app.command("backfill-schedule-codes")
def backfill_schedule_codes(
    limit: int = typer.Option(0, "--limit", help="Process at most N sections (0 = all)."),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Replace existing schedule_codes_json. Default merges (additive).",
    ),
    only_empty: bool = typer.Option(
        True,
        "--only-empty/--all-sections",
        help="Default: only sections whose schedule_codes_json is empty.",
    ),
    section_types: str = typer.Option(
        "rate_schedule,rider",
        "--section-types",
        help="Comma-separated section_type filter (default: rate_schedule,rider).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would change."),
    progress: bool = typer.Option(True, "--progress/--no-progress"),
) -> None:
    """Extract schedule/rider codes from section text and backfill document_sections.

    Default behavior:
      - Only sections with empty schedule_codes_json (12k of 14k).
      - Only sections classified as rate_schedule or rider.
      - Additive merge: existing codes are preserved; new codes are appended.
    """
    import json as _json

    from duke_rates.db.sqlite import connect
    from duke_rates.document_intelligence.schedule_code_extractor import (
        extract_codes,
    )
    from duke_rates.document_intelligence.section_text_extractor import (
        fetch_section_text,
    )

    settings, _ = _bootstrap()

    types = [t.strip() for t in section_types.split(",") if t.strip()]
    if not types:
        typer.echo("--section-types must be non-empty", err=True)
        raise typer.Exit(code=1)
    placeholders = ",".join("?" for _ in types)

    where_extra = ""
    if only_empty and not overwrite:
        where_extra = (
            "AND (schedule_codes_json IS NULL OR schedule_codes_json IN ('[]','null',''))"
        )

    conn = connect(settings.database_path)
    try:
        rows = conn.execute(
            f"""
            SELECT id, source_pdf, section_index, start_page, end_page,
                   schedule_codes_json
            FROM document_sections
            WHERE section_type IN ({placeholders})
              {where_extra}
            ORDER BY id
            """,
            types,
        ).fetchall()
    finally:
        conn.close()

    sections = [dict(r) for r in rows]
    if limit > 0:
        sections = sections[:limit]
    if not sections:
        typer.echo("No sections match the filter.")
        return

    typer.echo(
        f"Backfilling schedule_codes_json for {len(sections)} sections "
        f"(types={types}, overwrite={overwrite}, only_empty={only_empty})..."
    )
    if dry_run:
        typer.echo("[DRY RUN — no rows will be written]")

    n_updated = n_skipped = n_no_codes = n_fail = 0
    code_freq: dict[str, int] = {}

    for i, sec in enumerate(sections):
        rconn = connect(settings.database_path)
        try:
            sec_text = fetch_section_text(
                rconn,
                sec["source_pdf"],
                int(sec["start_page"]),
                int(sec["end_page"]),
                max_chars=2000,
            )
        finally:
            rconn.close()

        if not sec_text.text.strip():
            n_skipped += 1
            continue

        try:
            extraction = extract_codes(sec_text.text)
        except Exception:
            n_fail += 1
            continue

        if not extraction.codes:
            n_no_codes += 1
            continue

        try:
            existing = _json.loads(sec["schedule_codes_json"] or "[]")
            if not isinstance(existing, list):
                existing = []
        except Exception:
            existing = []
        existing_set = {str(c).upper() for c in existing}

        if overwrite:
            new_codes = list(extraction.codes)
        else:
            new_codes = list(existing) + [
                c for c in extraction.codes if c.upper() not in existing_set
            ]

        if new_codes == existing and not overwrite:
            n_skipped += 1
            continue

        for c in new_codes:
            code_freq[str(c).upper()] = code_freq.get(str(c).upper(), 0) + 1

        if not dry_run:
            wconn = connect(settings.database_path)
            try:
                wconn.execute(
                    "UPDATE document_sections SET schedule_codes_json = ? WHERE id = ?",
                    (_json.dumps(new_codes), int(sec["id"])),
                )
                wconn.commit()
            finally:
                wconn.close()
        n_updated += 1

        if progress and (i + 1) % 500 == 0:
            typer.echo(
                f"  {i + 1}/{len(sections)} updated={n_updated} no_codes={n_no_codes}",
                err=True,
            )

    typer.echo(
        f"\nDone: updated={n_updated} no_codes={n_no_codes} skipped={n_skipped} fail={n_fail}"
    )
    if code_freq:
        typer.echo("\nTop 15 codes extracted:")
        for code, n in sorted(code_freq.items(), key=lambda x: -x[1])[:15]:
            typer.echo(f"  {code:<25} {n}")
