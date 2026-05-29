"""Lineage sub-app: tariff family + historical document lifecycle.

Includes list/show, validate/suggest, promote/retire, repair/migrate/canonicalize,
deduplicate, and backfill commands for the tariff_families, historical_documents,
and tariff_versions tables.

Wired into the main CLI as `duke-rates lineage <command>`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import typer

from duke_rates.config import get_settings
from duke_rates.db.repository import Repository
from duke_rates.db.sqlite import connect as connect_sqlite
from duke_rates.download.hashing import sha256_bytes
from duke_rates.historical.ncuc.manual_registration import suggest_registration_metadata
from duke_rates.models.document import DocumentCategory, DocumentKind
from duke_rates.models.historical import HistoricalDocumentRecord

from duke_rates.cli_commands._cli_utils import _bootstrap, _safe_cli_text


lineage_app = typer.Typer(help="Tariff family + historical document lifecycle: list, repair, dedup, canonicalize, retire.")


# -------------------------------------------------------------------------
# Private helpers (only lineage commands use these)
# -------------------------------------------------------------------------

def _infer_canonical_family_key(family_key: str, title: str, company: str) -> str:
    """Infer a canonical family key from document title and existing key."""
    import re

    title_lower = title.lower()
    key_lower = family_key.lower()

    # Extract leaf number if present
    leaf_match = re.search(r"leaf\s*(?:no\.?\s*)?(\d{1,4})", title_lower)
    leaf_no = leaf_match.group(1) if leaf_match else None

    company_prefix = "nc-progress" if "progress" in key_lower else (
        "nc-carolinas" if "carolinas" in key_lower else "nc"
    )

    # Detect schedule patterns
    schedule_patterns = [
        (r"schedule\s+rs\b", f"{company_prefix}-schedule-rs"),
        (r"schedule\s+re\b", f"{company_prefix}-schedule-re"),
        (r"schedule\s+r[-\s]?tou", f"{company_prefix}-schedule-r-tou"),
        (r"schedule\s+r[-\s]?toud?\b", f"{company_prefix}-schedule-r-toud"),
        (r"schedule\s+res\b", f"{company_prefix}-schedule-res"),
        (r"schedule\s+sgs[-\s]?toue?\b", f"{company_prefix}-schedule-sgs-toue"),
        (r"schedule\s+sgs\b", f"{company_prefix}-schedule-sgs"),
        (r"schedule\s+lgs[-\s]?toue?\b", f"{company_prefix}-schedule-lgs-toue"),
        (r"schedule\s+lgs\b", f"{company_prefix}-schedule-lgs"),
        (r"schedule\s+pg\b", f"{company_prefix}-schedule-pg"),
        (r"schedule\s+ts\b", f"{company_prefix}-schedule-ts"),
        (r"schedule\s+hlf\b", f"{company_prefix}-schedule-hlf"),
        (r"schedule\s+i\b", f"{company_prefix}-schedule-i"),
        (r"schedule\s+fl\b", f"{company_prefix}-schedule-fl"),
        (r"schedule\s+wc\b", f"{company_prefix}-schedule-wc"),
        (r"schedule\s+nm\b", f"{company_prefix}-schedule-nm"),
        (r"schedule\s+ol\b", f"{company_prefix}-schedule-ol"),
        (r"schedule\s+se\b", f"{company_prefix}-schedule-se"),
        (r"schedule\s+lp\b", f"{company_prefix}-schedule-lp"),
        (r"schedule\s+isl?\b", f"{company_prefix}-schedule-is"),
        (r"schedule\s+dsm\b", f"{company_prefix}-schedule-dsm"),
        (r"schedule\s+ee\b", f"{company_prefix}-schedule-ee"),
        (r"schedule\s+opt[-\s]?e\b", f"{company_prefix}-schedule-opte"),
        (r"schedule\s+opt[-\s]?h\b", f"{company_prefix}-schedule-opth"),
        (r"schedule\s+opt[-\s]?g\b", f"{company_prefix}-schedule-optg"),
        (r"schedule\s+cpp\b", f"{company_prefix}-schedule-cpp"),
        (r"schedule\s+fcar\b", f"{company_prefix}-schedule-fcar"),
        (r"schedule\s+edpr\b", f"{company_prefix}-schedule-edpr"),
        (r"schedule\s+sbes\b", f"{company_prefix}-schedule-sbes"),
        (r"schedule\s+iqheu\b", f"{company_prefix}-schedule-iqheu"),
        (r"schedule\s+gs\b", f"{company_prefix}-schedule-gs"),
    ]

    for pattern, canonical in schedule_patterns:
        if re.search(pattern, title_lower):
            if leaf_no:
                return f"{canonical}-leaf-{leaf_no}"
            return canonical

    # Detect rider patterns
    rider_match = re.search(r"rider\s+(\w[\w\s-]{0,20})", title_lower)
    if rider_match:
        rider_code = rider_match.group(1).strip().upper().replace(" ", "-")[:15]
        return f"{company_prefix}-rider-{rider_code}"

    # Fallback: extract meaningful part from doc-* key
    doc_part = re.sub(r"^nc-(?:progress|carolinas)-doc-", "", family_key, flags=re.IGNORECASE)
    doc_part = re.sub(r"[^a-zA-Z0-9_-]", "", doc_part)[:60].strip("-").lower()
    if doc_part:
        if leaf_no:
            return f"{company_prefix}-schedule-{doc_part}-leaf-{leaf_no}"
        return f"{company_prefix}-schedule-{doc_part}"

    return family_key  # No inference possible


def _apply_canonicalization(conn: sqlite3.Connection, old_key: str, new_key: str) -> None:
    """Migrate all rows from old family key to new key, creating target family if needed."""
    target = conn.execute(
        "SELECT family_key FROM tariff_families WHERE family_key = ?", (new_key,)
    ).fetchone()

    if not target:
        # Create the canonical family with metadata from the old one
        old = conn.execute(
            "SELECT state, company, title, category FROM tariff_families WHERE family_key = ?",
            (old_key,),
        ).fetchone()
        if old:
            conn.execute(
                """INSERT INTO tariff_families (family_key, state, company, title, category, is_provisional, is_curated)
                   VALUES (?, ?, ?, ?, ?, 0, 1)""",
                (new_key, old["state"], old["company"], old["title"], old["category"]),
            )

    # Update all referencing tables
    for table, col in [
        ("historical_documents", "family_key"),
        ("tariff_versions", "family_key"),
        ("tariff_families", "family_key"),
        ("historical_reprocess_queue", "family_key"),
        ("ncuc_discovery_records", "family_key"),
        ("ncuc_missing_doc_targets", "family_key"),
    ]:
        try:
            conn.execute(
                f"UPDATE {table} SET {col} = ? WHERE {col} = ?",
                (new_key, old_key),
            )
        except sqlite3.OperationalError:
            pass  # Column may not exist in some tables

    # Update historical_processing_runs via historical_documents
    conn.execute(
        """UPDATE historical_processing_runs SET family_key = ?
           WHERE historical_document_id IN (
               SELECT id FROM historical_documents WHERE family_key = ?
           )""",
        (new_key, new_key),
    )

    # Delete old family if it was a doc-* key (and different from new)
    if old_key != new_key and "doc-" in old_key.lower():
        conn.execute("DELETE FROM tariff_families WHERE family_key = ?", (old_key,))


def _detect_anomaly_repair(doc: dict, run: dict | None) -> str:
    """Auto-detect the best repair action for an anomalous document."""
    if not run:
        return "enqueue_ocr"

    status = run["status"] or ""
    profile = run["parser_profile"] or ""
    metadata = json.loads(run["metadata_json"] or "{}")
    signals = metadata.get("signals") or {}
    text_len = (metadata.get("text_metrics") or {}).get("text_length") or 0

    # Skipped docs are accepted caveats
    if status.startswith("skipped"):
        return "accept_caveat"

    # Profile mismatch detection (before text check — signals are authoritative)
    has_carolinas = signals.get("has_carolinas_company_text")
    has_progress = signals.get("has_progress_company_text")
    company = (doc.get("company") or "").lower()

    progress_profile_on_carolinas = (
        profile.startswith("progress_") and (has_carolinas or "carolinas" in company)
    )
    carolinas_profile_on_progress = (
        profile.startswith("carolinas_") and (has_progress or "progress" in company)
    )
    if progress_profile_on_carolinas or carolinas_profile_on_progress:
        return "reassign_profile"

    # No text -> OCR
    if not text_len or text_len == 0:
        return "enqueue_ocr"

    # Weak parse with text -> likely parser limitation
    if run["outcome_quality"] == "weak":
        return "accept_caveat"

    # Fallback: if profile is unknown, try OCR
    if profile == "unknown":
        return "enqueue_ocr"

    return "accept_caveat"


# -------------------------------------------------------------------------
# Commands
# -------------------------------------------------------------------------

@lineage_app.command("list-tariff-families")
def list_tariff_families(
    state: str | None = typer.Option(None, help="Filter by state."),
    company: str | None = typer.Option(None, help="Filter by company."),
    family_type: str | None = typer.Option(None, help="Filter by type: rate_schedule, rider, etc."),
) -> None:
    """List tariff families in the database."""
    _, repository = _bootstrap()
    families = repository.list_tariff_families(state=state, company=company, family_type=family_type)
    from collections import Counter
    type_counts = Counter(f.family_type for f in families)
    typer.echo(f"Total: {len(families)} families")
    for ftype, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        typer.echo(f"  {ftype}: {count}")
    typer.echo("")
    for f in families[:50]:
        typer.echo(
            f"  {f.family_key:<45} {f.family_type:<15} {f.schedule_code or '?':<20} {(f.title or '')[:40]}"
        )
    if len(families) > 50:
        typer.echo(f"  ... and {len(families) - 50} more")


@lineage_app.command("list-provisional-families")
def list_provisional_families(
    state: str | None = typer.Option(None, help="Filter by state."),
    company: str | None = typer.Option(None, help="Filter by company."),
) -> None:
    """List provisional historical tariff families awaiting review/promotion."""
    _, repository = _bootstrap()
    families = repository.list_provisional_tariff_families(state=state, company=company)
    typer.echo(f"Total provisional families: {len(families)}")
    for family in families:
        typer.echo(
            f"  {family.family_key:<55} {family.family_type:<12} "
            f"{(family.schedule_code or '?'):<28} {(family.title or '')[:50]}"
        )


@lineage_app.command("show-provisional-review-candidates-nc")
def show_provisional_review_candidates_nc(
    state: str = typer.Option("NC", help="State filter."),
    company: str | None = typer.Option(None, help="Company filter."),
    family_key: str | None = typer.Option(None, "--family-key", help="Filter to one provisional family."),
    limit: int = typer.Option(25, "--limit", help="Maximum rows to display."),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON."),
) -> None:
    """Rank provisional NC families that still need manual review despite having charges."""
    _, repository = _bootstrap()
    rows = repository.score_provisional_tariff_families(
        state=state,
        company=company,
        family_key=family_key,
        limit=limit,
    )
    if json_out:
        typer.echo(json.dumps(rows, indent=2))
        return

    typer.echo(f"Provisional review candidates: {len(rows)}")
    for row in rows:
        typer.echo(
            f"  score={row['review_score']:<3} band={row['review_band']:<6} "
            f"charges={row['charge_count']:<3} quality={row['charge_quality_score']:.2f} "
            f"{row['family_key']}"
        )
        typer.echo(
            f"    current={row['family_type'] or '?'} / {(row['schedule_code'] or '?')} / {(row['title'] or '')[:80]}"
        )
        typer.echo(
            f"    suggest={row['suggested_family_type'] or '?'} / "
            f"{row['suggested_schedule_code'] or '?'} / {(row['suggested_title'] or '')[:80]}"
        )
        typer.echo(
            f"    action={row['recommended_action']} reasons={', '.join(row['review_reasons']) or '-'}"
        )
        if row.get("promotion_command"):
            typer.echo(f"    promote={row['promotion_command']}")


@lineage_app.command("show-gaps-nc")
def show_lineage_gaps_nc(
    limit: int = typer.Option(25, "--limit", help="Max rows to show per section."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show compact NC lineage gaps across discovery records, historical docs, versions, and families."""
    from duke_rates.historical.ncuc.lineage_gaps import build_lineage_gap_report

    _, repository = _bootstrap()
    report = build_lineage_gap_report(repository, limit=limit)

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    summary = report["summary"]
    typer.echo("Lineage Gaps (NC)")
    typer.echo(
        "  "
        f"unlinked_discovery={summary['unlinked_discovery_records_count']}  "
        f"auto_matchable_discovery={summary['auto_matchable_discovery_records_count']}"
    )
    typer.echo(
        "  "
        f"historical_missing_effective_start={summary['historical_missing_effective_start_count']}  "
        f"historical_missing_version_link={summary['historical_missing_version_count']}"
    )
    typer.echo(
        "  "
        f"versions_missing_historical_document_id={summary['versions_missing_historical_document_id_count']}  "
        f"families_without_charges={summary['families_without_charges_count']}"
    )

    typer.echo("\nAuto-Matchable Discovery Records")
    for row in report["auto_matchable_discovery_records"]:
        top_match = row["top_match"]
        typer.echo(
            "  "
            f"id={row['discovery_record_id']} "
            f"family={top_match['family_key']} "
            f"score={top_match['score']} "
            f"reasons={','.join(top_match['reasons'])}"
        )

    typer.echo("\nHistorical Docs Missing effective_start")
    for row in report["historical_missing_effective_start"]:
        typer.echo(
            "  "
            f"id={row['id']} family={row['family_key']} company={row['company'] or '-'} "
            f"title={(row['title'] or '')[:50]}"
        )

    typer.echo("\nHistorical Docs Missing tariff_version Link")
    for row in report["historical_missing_version_link"]:
        typer.echo(
            "  "
            f"id={row['id']} family={row['family_key']} eff={row['effective_start']} "
            f"title={(row['title'] or '')[:50]}"
        )

    typer.echo("\nTariff Versions Missing historical_document_id")
    for row in report["versions_missing_historical_document_id"]:
        typer.echo(
            "  "
            f"id={row['id']} family={row['family_key']} company={row['company'] or '-'} "
            f"eff={row['effective_start'] or '-'} source={row['source_type']}"
        )

    typer.echo("\nFamilies Without Charges")
    for row in report["families_without_charges"]:
        typer.echo(
            "  "
            f"family={row['family_key']} company={row['company'] or '-'} "
            f"versions={row['version_count']} historical_docs={row['historical_document_count']}"
        )


def _repair_lineage_effective_start_gaps(
    conn: sqlite3.Connection,
    *,
    dry_run: bool,
    limit: int = 100,
) -> dict[str, object]:
    """Fill missing effective_start from same-PDF siblings with one known date."""
    started_at = datetime.now()
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    capped_limit = max(0, int(limit or 0))
    limit_sql = "" if capped_limit == 0 else "LIMIT ?"
    params: tuple[object, ...] = () if capped_limit == 0 else (capped_limit,)
    candidates = conn.execute(
        f"""
        WITH pdf_dates AS (
            SELECT
                local_path,
                COUNT(DISTINCT effective_start) AS known_date_count,
                MAX(effective_start) AS inferred_effective_start
            FROM historical_documents
            WHERE state = 'NC'
              AND local_path IS NOT NULL
              AND COALESCE(effective_start, '') <> ''
              AND SUBSTR(effective_start, 1, 4) GLOB '[12][0-9][0-9][0-9]'
            GROUP BY local_path
        )
        SELECT
            hd.id AS historical_document_id,
            hd.family_key,
            hd.title,
            hd.local_path,
            pdf_dates.inferred_effective_start
        FROM historical_documents hd
        JOIN pdf_dates
          ON pdf_dates.local_path = hd.local_path
        WHERE hd.state = 'NC'
          AND hd.local_path IS NOT NULL
          AND (hd.effective_start IS NULL OR hd.effective_start = '')
          AND pdf_dates.known_date_count = 1
        ORDER BY hd.id DESC
        {limit_sql}
        """,
        params,
    ).fetchall()

    result: dict[str, object] = {
        "dry_run": dry_run,
        "candidates_found": len(candidates),
        "effective_starts_repaired": 0,
        "versions_linked": 0,
        "versions_created": 0,
        "skipped_existing_linked_version": 0,
        "skipped_ambiguous_version": 0,
        "errors": [],
        "per_doc": [],
        "duration_ms": 0,
    }

    for row in candidates:
        hd_id = int(row["historical_document_id"])
        family_key = str(row["family_key"])
        effective_start = str(row["inferred_effective_start"])
        action = "create_version"
        version_id: int | None = None

        try:
            version_rows = conn.execute(
                """
                SELECT id, historical_document_id
                FROM tariff_versions
                WHERE family_key = ?
                  AND effective_start = ?
                ORDER BY id
                """,
                (family_key, effective_start),
            ).fetchall()
            unlinked_versions = [item for item in version_rows if item["historical_document_id"] is None]
            linked_versions = [item for item in version_rows if item["historical_document_id"] is not None]

            if len(unlinked_versions) == 1 and not linked_versions:
                action = "link_version"
                version_id = int(unlinked_versions[0]["id"])
            elif not version_rows:
                action = "create_version"
            elif linked_versions:
                action = "skip_existing_linked_version"
                result["skipped_existing_linked_version"] = int(result["skipped_existing_linked_version"]) + 1
            else:
                action = "skip_ambiguous_version"
                result["skipped_ambiguous_version"] = int(result["skipped_ambiguous_version"]) + 1

            if action.startswith("skip_"):
                per_doc_action = action
            else:
                per_doc_action = action
                result["effective_starts_repaired"] = int(result["effective_starts_repaired"]) + 1
                if not dry_run:
                    conn.execute(
                        """
                        UPDATE historical_documents
                        SET effective_start = ?
                        WHERE id = ?
                        """,
                        (effective_start, hd_id),
                    )

                if action == "link_version":
                    result["versions_linked"] = int(result["versions_linked"]) + 1
                    if not dry_run:
                        conn.execute(
                            """
                            UPDATE tariff_versions
                            SET historical_document_id = ?
                            WHERE id = ?
                            """,
                            (hd_id, version_id),
                        )
                else:
                    result["versions_created"] = int(result["versions_created"]) + 1
                    if not dry_run:
                        cur = conn.execute(
                            """
                            INSERT INTO tariff_versions (
                                family_key, historical_document_id, effective_start,
                                source_type, confidence_score, notes, created_at
                            ) VALUES (?, ?, ?, 'regulator', 0.85, ?, ?)
                            """,
                            (
                                family_key,
                                hd_id,
                                effective_start,
                                "Inferred by repair-lineage-gaps-nc from single-date source PDF.",
                                now,
                            ),
                        )
                        version_id = int(cur.lastrowid)

            result["per_doc"].append(
                {
                    "historical_document_id": hd_id,
                    "family_key": family_key,
                    "title": row["title"],
                    "local_path": row["local_path"],
                    "effective_start": effective_start,
                    "action": per_doc_action,
                    "version_id": version_id,
                }
            )
        except Exception as exc:  # pragma: no cover - defensive per-row isolation
            result["errors"].append({"historical_document_id": hd_id, "error": str(exc)})

    if not dry_run:
        conn.commit()
    result["duration_ms"] = int((datetime.now() - started_at).total_seconds() * 1000)
    return result


@lineage_app.command("repair-lineage-gaps-nc")
def repair_lineage_gaps_nc(
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying DB (default: dry-run)."),
    limit: int = typer.Option(100, "--limit", help="Max deterministic repairs to process (0 = all)."),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Repair deterministic NC lineage gaps.

    Currently fixes historical documents missing effective_start when all
    dated sibling spans from the same source PDF agree on exactly one date,
    then links or creates the matching regulator tariff_version.
    """
    _, repository = _bootstrap()
    conn = connect_sqlite(repository.database_path)
    try:
        result = _repair_lineage_effective_start_gaps(conn, dry_run=dry_run, limit=limit)
    finally:
        conn.close()

    if json_out:
        typer.echo(json.dumps(result, indent=2, default=str))
        return

    moved = int(result["effective_starts_repaired"])
    typer.echo(f"\nLineage Gap Repair {'(DRY RUN)' if dry_run else '(EXECUTED)'}")
    typer.echo(f"  Candidates found:           {result['candidates_found']}")
    typer.echo(f"  Effective starts repaired:  {result['effective_starts_repaired']}")
    typer.echo(f"  Versions linked:            {result['versions_linked']}")
    typer.echo(f"  Versions created:           {result['versions_created']}")
    typer.echo(f"  Skipped existing linked:    {result['skipped_existing_linked_version']}")
    typer.echo(f"  Skipped ambiguous version:  {result['skipped_ambiguous_version']}")
    typer.echo(f"  Repaired lineage gaps: moved={moved}")

    if result["errors"]:
        typer.echo(f"\n  Errors ({len(result['errors'])}):")
        for item in result["errors"][:10]:
            typer.echo(f"    - hd:{item['historical_document_id']} {item['error']}")

    for item in result["per_doc"][:10]:
        typer.echo(
            "  "
            f"hd:{item['historical_document_id']} action={item['action']} "
            f"family={item['family_key']} eff={item['effective_start']}"
        )

    typer.echo(f"\n  Duration: {result['duration_ms']}ms")
    if dry_run and moved > 0:
        typer.echo("\n  Re-run with --execute to apply changes.")


@lineage_app.command("show-provenance-gaps-nc")
def show_provenance_gaps_nc(
    limit: int = typer.Option(25, "--limit", help="Max rows to show per section."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show NC provenance gaps across tariff versions and discovery linkage."""
    from duke_rates.historical.ncuc.provenance_gaps import build_provenance_gap_report

    _, repository = _bootstrap()
    report = build_provenance_gap_report(repository, limit=limit)

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    summary = report["summary"]
    typer.echo("Provenance Gaps (NC)")
    typer.echo(
        "  "
        f"historical_versions={summary['historical_versions_count']}  "
        f"versions_missing_any={summary['versions_missing_any_provenance_count']}"
    )
    typer.echo(
        "  "
        f"missing_docket_number={summary['versions_missing_docket_number_count']}  "
        f"missing_order_date={summary['versions_missing_order_date_count']}  "
        f"missing_leaf_no={summary['versions_missing_leaf_no_count']}"
    )
    typer.echo(
        "  "
        f"missing_source_pdf={summary['versions_missing_source_pdf_count']}  "
        f"missing_docket_dir={summary['versions_missing_docket_dir_count']}"
    )
    typer.echo(
        "  "
        f"historical_missing_discovery_match={summary['historical_documents_missing_discovery_match_count']}  "
        f"path_only_link={summary['historical_documents_path_only_discovery_link_count']}  "
        f"hash_only_link={summary['historical_documents_hash_only_discovery_link_count']}"
    )
    typer.echo(
        "  "
        f"acquired_discovery_missing_docket={summary['acquired_discovery_records_missing_docket_number_count']}"
    )

    typer.echo("\nTariff Versions Missing Provenance")
    if not report["versions_missing_provenance"]:
        typer.echo("  none")
    for row in report["versions_missing_provenance"]:
        typer.echo(
            "  "
            f"id={row['id']} family={row['family_key']} company={row['company'] or '-'} "
            f"missing={','.join(row['missing_fields'])} linkage={row['discovery_linkage']}"
        )
        if row["candidate_fill_fields"]:
            typer.echo(f"    candidate_fill={','.join(row['candidate_fill_fields'])}")
        typer.echo(f"    title={(row['title'] or '')[:90]}")

    typer.echo("\nHistorical Docs Missing Discovery Match")
    if not report["historical_documents_missing_discovery_match"]:
        typer.echo("  none")
    for row in report["historical_documents_missing_discovery_match"]:
        typer.echo(
            "  "
            f"id={row['id']} family={row['family_key']} company={row['company'] or '-'} "
            f"eff={row['effective_start'] or '-'} leaf={row['leaf_no'] or '-'}"
        )
        typer.echo(f"    title={(row['title'] or '')[:90]}")

    typer.echo("\nHistorical Docs With Path-Only Discovery Link")
    if not report["historical_documents_path_only_discovery_link"]:
        typer.echo("  none")
    for row in report["historical_documents_path_only_discovery_link"]:
        typer.echo(
            "  "
            f"id={row['id']} family={row['family_key']} company={row['company'] or '-'} "
            f"matched_discovery={row['matched_discovery_record_id'] or '-'} "
            f"docket={row['matched_discovery_docket_number'] or '-'}"
        )
        typer.echo(f"    title={(row['title'] or '')[:90]}")

    typer.echo("\nAcquired Discovery Rows Missing docket_number")
    if not report["acquired_discovery_records_missing_docket_number"]:
        typer.echo("  none")
    for row in report["acquired_discovery_records_missing_docket_number"]:
        typer.echo(
            "  "
            f"id={row['id']} status={row['fetch_status']} date={row['filing_date'] or '-'} "
            f"utility={row['utility'] or '-'}"
        )
        typer.echo(f"    title={(row['filing_title'] or '')[:90]}")


@lineage_app.command("show-fingerprint-coverage-nc")
def show_fingerprint_coverage_nc(
    limit: int = typer.Option(25, "--limit", help="Max rows to show per section."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show NC fingerprint/hash coverage across historical docs and reusable artifacts."""
    from duke_rates.historical.ncuc.fingerprint_coverage import build_fingerprint_coverage_report

    _, repository = _bootstrap()
    report = build_fingerprint_coverage_report(repository, limit=limit)

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    summary = report["summary"]
    typer.echo("Fingerprint Coverage (NC)")
    typer.echo(
        "  "
        f"historical_total={summary['historical_nc_total_count']}  "
        f"hash_backed={summary['historical_nc_hash_backed_count']}  "
        f"path_only={summary['historical_nc_path_only_count']}"
    )
    typer.echo(
        "  "
        f"historical_with_fingerprint={summary['historical_nc_with_fingerprint_count']}  "
        f"historical_without_fingerprint={summary['historical_nc_without_fingerprint_count']}  "
        f"hash_backed_with_fingerprint={summary['historical_nc_hash_backed_with_fingerprint_count']}"
    )
    typer.echo(
        "  "
        f"historical_with_page_artifacts={summary['historical_nc_with_page_artifacts_count']}  "
        f"historical_with_span_artifacts={summary['historical_nc_with_span_artifacts_count']}  "
        f"historical_with_docling={summary['historical_nc_with_docling_count']}  "
        f"historical_with_ocr={summary['historical_nc_with_ocr_count']}"
    )
    typer.echo(
        "  "
        f"acquired_discovery_total={summary['acquired_discovery_total_count']}  "
        f"acquired_with_hash={summary['acquired_discovery_with_hash_count']}"
    )
    typer.echo(
        "  "
        f"acquired_with_page_artifacts={summary['acquired_discovery_with_page_artifacts_count']}  "
        f"acquired_with_span_artifacts={summary['acquired_discovery_with_span_artifacts_count']}  "
        f"acquired_with_docling={summary['acquired_discovery_with_docling_count']}  "
        f"acquired_with_ocr={summary['acquired_discovery_with_ocr_count']}"
    )
    typer.echo(
        "  "
        f"fingerprint_rows={summary['document_fingerprint_row_count']}  "
        f"rows_with_family_key={summary['fingerprint_rows_with_family_key_count']}  "
        f"rows_with_parser_profile={summary['fingerprint_rows_with_parser_profile_count']}  "
        f"rows_with_outcome_quality={summary['fingerprint_rows_with_outcome_quality_count']}"
    )

    typer.echo("\nHistorical Coverage By Company")
    for row in report["historical_by_company"]:
        typer.echo(
            "  "
            f"company={row['company'] or '-'} "
            f"historical_docs={row['historical_document_count']} "
            f"hash_backed={row['hash_backed_count']} "
            f"with_fingerprint={row['with_fingerprint_count']} "
            f"with_span_artifacts={row['with_span_artifacts_count']}"
        )

    typer.echo("\nFingerprint Outcome Quality")
    for row in report["fingerprint_quality_breakdown"]:
        typer.echo(
            "  "
            f"outcome_quality={row['outcome_quality']} rows={row['row_count']}"
        )

    typer.echo("\nHistorical Docs Without Fingerprint")
    if not report["historical_documents_without_fingerprint"]:
        typer.echo("  none")
    for row in report["historical_documents_without_fingerprint"]:
        typer.echo(
            "  "
            f"id={row['id']} family={row['family_key']} company={row['company'] or '-'} "
            f"eff={row['effective_start'] or '-'}"
        )
        typer.echo(f"    title={(row['title'] or '')[:90]}")

    typer.echo("\nHash-Backed Historical Docs Without Fingerprint")
    if not report["hash_backed_historical_documents_without_fingerprint"]:
        typer.echo("  none")
    for row in report["hash_backed_historical_documents_without_fingerprint"]:
        typer.echo(
            "  "
            f"id={row['id']} family={row['family_key']} company={row['company'] or '-'} "
            f"eff={row['effective_start'] or '-'}"
        )
        typer.echo(f"    title={(row['title'] or '')[:90]}")


@lineage_app.command("validate-nc")
def validate_lineage_nc(
    limit: int = typer.Option(25, "--limit", help="Max issue rows to show."),
    family_key: str | None = typer.Option(None, help="Optional family key filter."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Cross-check NC historical docs for family assignment, provenance debt, and extraction readiness."""
    from duke_rates.historical.ncuc.lineage_validation import build_lineage_validation_report

    _, repository = _bootstrap()
    report = build_lineage_validation_report(repository, limit=limit, family_key=family_key)

    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    summary = report["summary"]
    typer.echo("Lineage Validation (NC)")
    typer.echo(
        "  "
        f"total_docs={summary['total_documents_count']}  "
        f"blocking={summary['blocking_issue_document_count']}  "
        f"warning_only={summary['warning_only_document_count']}  "
        f"clean={summary['clean_document_count']}"
    )
    typer.echo(
        "  "
        f"missing_tariff_family={summary['missing_tariff_family_count']}  "
        f"provisional_family={summary['provisional_family_count']}  "
        f"missing_effective_start={summary['missing_effective_start_count']}"
    )
    typer.echo(
        "  "
        f"missing_version_link={summary['missing_version_link_count']}  "
        f"not_processed={summary['not_processed_count']}  "
        f"linked_without_charges={summary['linked_without_charges_count']}"
    )
    typer.echo(
        "  "
        f"version_provenance_gap={summary['version_provenance_gap_count']}  "
        f"missing_discovery_match={summary['missing_discovery_match_count']}  "
        f"path_only_discovery_link={summary['path_only_discovery_link_count']}"
    )
    typer.echo(
        "  "
        f"extracted_with_charges={summary['extracted_with_charges_count']}  "
        f"skipped_reference={summary['skipped_reference_count']}"
    )

    for row in report["rows"]:
        issue_parts: list[str] = []
        if row["blocking_issues"]:
            issue_parts.append(f"blockers={','.join(row['blocking_issues'])}")
        if row["warning_issues"]:
            issue_parts.append(f"warnings={','.join(row['warning_issues'])}")
        typer.echo(
            "  "
            f"id={row['historical_document_id']} family={row['family_key'] or '-'} "
            f"company={row['company'] or '-'} {' '.join(issue_parts)}"
        )
        typer.echo(
            "    "
            f"eff={row['effective_start'] or '-'} "
            f"versions={row['version_count']} "
            f"charges={row['charge_count']} "
            f"latest_outcome={row['latest_outcome_quality'] or '-'} "
            f"linkage={row['discovery_linkage']}"
        )
        typer.echo(f"    title={(row['title'] or '')[:90]}")


@lineage_app.command("suggest-family-links-nc")
def suggest_family_links_nc(
    limit: int = typer.Option(25, "--limit", help="Max discovery records to show."),
    record_id: int | None = typer.Option(None, "--record-id", help="Only inspect one discovery record."),
    apply: bool = typer.Option(False, "--apply", help="Persist suggested family links back to discovery records."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Suggest likely NC family links for stranded discovery records using span clues."""
    from duke_rates.historical.ncuc.lineage_gaps import (
        apply_family_link_suggestions,
        suggest_family_links,
    )

    _, repository = _bootstrap()
    suggestions = suggest_family_links(repository, limit=limit, record_id=record_id)

    if apply:
        updated = apply_family_link_suggestions(repository, suggestions)
    else:
        updated = 0

    if json_out:
        payload = {
            "suggestion_count": len(suggestions),
            "updated_count": updated,
            "suggestions": [
                {
                    "discovery_record_id": item["discovery_record_id"],
                    "docket_number": item["docket_number"],
                    "utility": item["utility"],
                    "filing_title": item["filing_title"],
                    "leaf_nos": item["leaf_nos"],
                    "schedule_codes": item["schedule_codes"],
                    "family_keys": item["family_keys"],
                    "matches": item["matches"],
                }
                for item in suggestions
            ],
        }
        typer.echo(json.dumps(payload, indent=2, default=str))
        return

    typer.echo(f"Suggested family links: {len(suggestions)}")
    for item in suggestions:
        top_match = item["matches"][0]
        typer.echo(
            "  "
            f"id={item['discovery_record_id']} "
            f"family={top_match['family_key']} "
            f"score={top_match['score']} "
            f"reasons={','.join(top_match['reasons'])}"
        )
        typer.echo(f"    title={(item['filing_title'] or '')[:90]}")
        typer.echo(f"    leafs={item['leaf_nos']}")
        typer.echo(f"    codes={item['schedule_codes']}")

    if apply:
        typer.echo(f"\nUpdated {updated} discovery records.")


@lineage_app.command("promote-provisional-family")
def promote_provisional_family(
    family_key: str = typer.Argument(..., help="Existing provisional family_key."),
    title: str | None = typer.Option(None, help="Override curated title."),
    schedule_code: str | None = typer.Option(None, help="Override schedule_code."),
    family_type: str | None = typer.Option(None, help="Override family_type."),
    alias: list[str] | None = typer.Option(None, "--alias", help="Additional alias to retain."),
    notes: str | None = typer.Option(None, help="Override notes."),
) -> None:
    """Promote a provisional historical family into a curated tariff family."""
    _, repository = _bootstrap()
    promoted = repository.promote_provisional_tariff_family(
        family_key,
        title=title,
        schedule_code=schedule_code,
        family_type=family_type,
        aliases=alias,
        notes=notes,
    )
    if promoted is None:
        typer.echo(f"Family not found: {family_key}")
        raise typer.Exit(1)
    typer.echo(
        f"Promoted {promoted.family_key} | {promoted.family_type} | "
        f"{promoted.schedule_code or '?'} | {promoted.title or ''}"
    )


@lineage_app.command("list-historical-only-families")
def list_historical_only_families(
    state: str | None = typer.Option(None, help="Filter by state."),
    company: str | None = typer.Option(None, help="Filter by company."),
    family_type: str | None = typer.Option(None, help="Filter by family_type."),
    with_candidates: bool = typer.Option(True, help="Show suggested current-document candidates."),
    only_unresolved: bool = typer.Option(False, help="Show only families with no plausible current-document candidates."),
) -> None:
    """List tariff families backed only by historical documents and no current-document anchor."""
    _, repository = _bootstrap()
    rows = repository.review_historical_only_tariff_families(
        state=state,
        company=company,
        family_type=family_type,
    )
    if only_unresolved:
        rows = [row for row in rows if row["review_status"] == "unresolved"]
    typer.echo(f"Total historical-only families: {len(rows)}")
    unresolved_count = sum(1 for row in rows if row["review_status"] == "unresolved")
    candidate_count = len(rows) - unresolved_count
    typer.echo(
        f"  unresolved={unresolved_count} review_candidates={candidate_count}"
    )
    for row in rows:
        typer.echo(
            f"  {row['family_key']:<55} {row['family_type']:<12} "
            f"{(row['schedule_code'] or '?'):<28} hist_docs={row['historical_document_count']:<3} "
            f"{(row['title'] or '')[:40]} [{row['review_status']}]"
        )
        if with_candidates:
            for suggestion in row["suggestions"]:
                typer.echo(
                    f"    candidate doc={suggestion['document_id']:<4} score={suggestion['score']:<2} "
                    f"{suggestion['title']} [{', '.join(suggestion['reasons'])}]"
                )
                if suggestion.get("candidate_headings"):
                    typer.echo(
                        f"      headings: {', '.join(suggestion['candidate_headings'])}"
                    )


@lineage_app.command("list-weak-unbounded-historical-nc")
def list_weak_unbounded_historical_nc(
    state: str | None = typer.Option("NC", help="Filter by state."),
    company: str | None = typer.Option(None, help="Filter by company."),
    family_key: str | None = typer.Option(None, "--family-key", help="Filter by family key."),
    limit: int = typer.Option(50, help="Max rows to display."),
) -> None:
    """List weak historical docs that still point at whole PDFs instead of bounded spans."""
    _, repository = _bootstrap()
    rows = repository.list_weak_unbounded_historical_documents(
        state=state,
        company=company,
        family_key=family_key,
        limit=limit,
    )
    for row in rows:
        typer.echo(
            "\t".join(
                [
                    str(row["historical_document_id"]),
                    row["family_key"],
                    row["source_kind"],
                    row["review_action"],
                    str(row["discovery_record_id"] or "-"),
                    row["parser_profile"] or "-",
                    str(row["charge_count"]),
                    row["local_path"],
                ]
            )
        )


@lineage_app.command("list-redundant-legacy-raw-historical-nc")
def list_redundant_legacy_raw_historical_nc(
    state: str | None = typer.Option("NC", help="Filter by state."),
    company: str | None = typer.Option(None, help="Filter by company."),
    family_key: str | None = typer.Option(None, "--family-key", help="Filter by family key."),
    limit: int = typer.Option(100, help="Max rows to display."),
) -> None:
    """List weak legacy raw rows that already have bounded same-family regulator replacements."""
    _, repository = _bootstrap()
    rows = repository.list_redundant_legacy_raw_historical_documents(
        state=state,
        company=company,
        family_key=family_key,
        limit=limit,
    )
    for row in rows:
        typer.echo(
            "\t".join(
                [
                    str(row["historical_document_id"]),
                    row["family_key"],
                    str(row["discovery_record_id"] or "-"),
                    str(row["replacement_count"]),
                    ",".join(str(item) for item in row["replacement_ids"]),
                    row["local_path"],
                ]
            )
        )


@lineage_app.command("list-bundle-reference-legacy-raw-historical-nc")
def list_bundle_reference_legacy_raw_historical_nc(
    state: str | None = typer.Option("NC", help="Filter by state."),
    company: str | None = typer.Option(None, help="Filter by company."),
    family_key: str | None = typer.Option(None, "--family-key", help="Filter by family key."),
    limit: int = typer.Option(100, help="Max rows to display."),
) -> None:
    """List weak legacy raw rows that appear to be bundle rider references inside bounded spans."""
    _, repository = _bootstrap()
    rows = repository.list_bundle_reference_legacy_raw_historical_documents(
        state=state,
        company=company,
        family_key=family_key,
        limit=limit,
    )
    for row in rows:
        overlap = row.get("bundle_reference_overlap") or {}
        host_descriptions = []
        for host in overlap.get("hosts") or []:
            host_descriptions.append(
                f"{host['host_historical_document_id']}:{host['host_family_key']}@{host['host_start_page']}-{host['host_end_page']}"
            )
        typer.echo(
            "\t".join(
                [
                    str(row["historical_document_id"]),
                    row["family_key"],
                    str(row["discovery_record_id"] or "-"),
                    str(overlap.get("target_leaf") or "-"),
                    str(overlap.get("host_count") or 0),
                    ",".join(host_descriptions),
                    row["local_path"],
                ]
            )
        )


@lineage_app.command("list-placeholder-heading-historical-nc")
def list_placeholder_heading_historical_nc(
    state: str | None = typer.Option("NC", help="Filter by state."),
    company: str | None = typer.Option(None, help="Filter by company."),
    family_key: str | None = typer.Option(None, "--family-key", help="Filter by family key."),
    limit: int = typer.Option(100, help="Max rows to display."),
) -> None:
    """List bounded placeholder heading spans that can be retired as residue."""
    _, repository = _bootstrap()
    rows = repository.list_placeholder_heading_residue_historical_documents(
        state=state,
        company=company,
        family_key=family_key,
        limit=limit,
    )
    for row in rows:
        neighbors = ",".join(
            f"{item['historical_document_id']}:{item['family_key']}@{item['start_page']}-{item['end_page']}"
            for item in row["neighbors"]
        )
        typer.echo(
            "\t".join(
                [
                    str(row["historical_document_id"]),
                    row["family_key"],
                    f"{row['start_page']}-{row['end_page']}",
                    str(row["neighbor_count"]),
                    neighbors,
                    row["local_path"],
                ]
            )
        )


@lineage_app.command("retire-historical-document")
def retire_historical_document(
    historical_document_id: int = typer.Argument(..., help="Historical document id to retire."),
) -> None:
    """Delete a historical document row and its attached parse/extraction state."""
    _, repository = _bootstrap()
    retired = repository.retire_historical_document(historical_document_id)
    if not retired:
        typer.echo(f"Historical document not found: {historical_document_id}")
        raise typer.Exit(1)
    typer.echo(f"Retired historical document {historical_document_id}")


@lineage_app.command("add-historical-document-nc")
def add_historical_document_nc(
    family_key: str = typer.Option(..., "--family-key", help="Target NC family key."),
    local_path: Path = typer.Option(..., "--local-path", exists=True, file_okay=True, dir_okay=False, resolve_path=True, help="Local PDF path to register."),
    archived_url: str = typer.Option(..., "--archived-url", help="Canonical regulator/archive URL for this PDF or slice."),
    title: str | None = typer.Option(None, "--title", help="Stored historical document title (defaults to the filename stem)."),
    company: str = typer.Option(..., "--company", help="Utility company slug: progress or carolinas."),
    category: str = typer.Option(DocumentCategory.RATE.value, "--category", help="Historical category, e.g. rate, rider, tariff."),
    start_page: int | None = typer.Option(None, "--start-page", min=1, help="1-based start page for the tariff slice."),
    end_page: int | None = typer.Option(None, "--end-page", min=1, help="1-based end page for the tariff slice."),
    effective_start: str | None = typer.Option(None, "--effective-start", help="Effective start date YYYY-MM-DD."),
    effective_end: str | None = typer.Option(None, "--effective-end", help="Effective end date YYYY-MM-DD."),
    revision_label: str | None = typer.Option(None, "--revision-label", help="Revision label stored on the historical row."),
    supersedes_label: str | None = typer.Option(None, "--supersedes-label", help="Supersedes label stored on the historical row."),
    leaf_no: str | None = typer.Option(None, "--leaf-no", help="Leaf number stored on the historical row."),
    canonical_url: str | None = typer.Option(None, "--canonical-url", help="Optional canonical source URL if different from --archived-url."),
    auto_detect: bool = typer.Option(False, "--auto-detect", help="Infer page bounds and footer metadata from the PDF before registration."),
) -> None:
    """Register one page-bounded NC historical PDF directly into historical_documents."""
    if local_path.suffix.lower() != ".pdf":
        raise typer.BadParameter("--local-path must point to a PDF.")
    if end_page is not None and start_page is None:
        raise typer.BadParameter("--end-page requires --start-page.")
    if start_page is not None and end_page is not None and end_page < start_page:
        raise typer.BadParameter("--end-page must be greater than or equal to --start-page.")

    _, repository = _bootstrap()
    if auto_detect:
        suggestion = suggest_registration_metadata(
            repository,
            family_key=family_key,
            pdf_path=local_path,
        )
        if suggestion is None and start_page is None:
            raise typer.BadParameter(
                "--auto-detect could not identify a tariff slice; provide --start-page/--end-page manually."
            )
        if suggestion is not None:
            start_page = start_page or suggestion.start_page
            end_page = end_page or suggestion.end_page
            effective_start = effective_start or suggestion.effective_start
            supersedes_label = supersedes_label or suggestion.supersedes_label
            leaf_no = leaf_no or suggestion.leaf_no
            title = title or suggestion.title
            typer.echo(
                f"Auto-detected pages={suggestion.start_page}-{suggestion.end_page} "
                f"effective_start={suggestion.effective_start or '-'} "
                f"supersedes={suggestion.supersedes_label or '-'} "
                f"docket={suggestion.docket_number or '-'} "
                f"confidence={suggestion.confidence:.2f}"
            )

    now = datetime.now()
    raw_text_path = local_path.with_suffix(local_path.suffix + ".txt")
    record = HistoricalDocumentRecord(
        family_key=family_key,
        title=title or local_path.stem,
        state="NC",
        company=company,
        category=category,
        kind=DocumentKind.PDF.value,
        canonical_url=canonical_url or archived_url,
        archived_url=archived_url,
        snapshot_timestamp=now,
        local_path=local_path,
        raw_text_path=raw_text_path if raw_text_path.exists() else None,
        content_hash=sha256_bytes(local_path.read_bytes()),
        content_type="application/pdf",
        direct_status_code=200,
        direct_downloadable=True,
        revision_label=revision_label,
        supersedes_label=supersedes_label,
        leaf_no=leaf_no,
        start_page=start_page,
        end_page=end_page,
        effective_start=effective_start,
        effective_end=effective_end,
        retrieved_at=now,
    )
    historical_id = repository.upsert_historical_document(record)
    typer.echo(
        f"Registered historical document {historical_id} family={family_key} "
        f"pages={start_page or '-'}-{end_page or start_page or '-'} "
        f"effective_start={effective_start or '-'}"
    )


@lineage_app.command("rebind-historical-page-range")
def rebind_historical_page_range(
    historical_document_id: int = typer.Argument(..., help="Historical document id to update."),
    start_page: int = typer.Option(..., "--start-page", min=1, help="New 1-based start page."),
    end_page: int | None = typer.Option(None, "--end-page", min=1, help="New 1-based end page (defaults to start page)."),
    requeue: bool = typer.Option(False, "--requeue", help="Queue the document for re-extraction after rebinding."),
    requested_by: str = typer.Option("operator", "--requested-by", help="Queue requester label when --requeue is used."),
    queue_priority: int = typer.Option(90, "--queue-priority", help="Queue priority when --requeue is used."),
) -> None:
    """Update an existing historical document's page bounds and optionally requeue it."""
    _, repository = _bootstrap()
    try:
        rebound = repository.rebind_historical_page_range(
            historical_document_id,
            start_page=start_page,
            end_page=end_page,
            requeue=requeue,
            requested_by=requested_by,
            queue_priority=queue_priority,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if rebound is None:
        typer.echo(f"Historical document not found: {historical_document_id}")
        raise typer.Exit(1)
    typer.echo(
        f"Rebound historical document {historical_document_id} -> "
        f"pages {rebound.start_page}-{rebound.end_page or rebound.start_page}"
    )
    if requeue:
        typer.echo(f"Queued for reprocess with priority={queue_priority} requested_by={requested_by}")


@lineage_app.command("clear-redline-fingerprint")
def clear_redline_fingerprint(
    historical_document_id: int = typer.Option(..., "--hd-id", help="Historical document id whose fingerprint slice should be cleared."),
    include_path_rollup: bool = typer.Option(False, "--include-path-rollup", help="Also clear whole-PDF path-level fingerprint rows for the same source PDF."),
    force: bool = typer.Option(False, "--force", help="Apply the clear. Without --force this command only previews the target."),
) -> None:
    """Clear a stored redline verdict for a historical-document slice."""
    _, repository = _bootstrap()
    target = repository.get_historical_document(historical_document_id)
    if target is None:
        typer.echo(f"Historical document not found: {historical_document_id}")
        raise typer.Exit(1)
    if not force:
        typer.echo(
            f"[DRY RUN] Would clear redline fingerprint for hd={historical_document_id} "
            f"{target.local_path} pages {target.start_page}-{target.end_page or target.start_page}"
        )
        if include_path_rollup:
            typer.echo("  whole-PDF path-level fingerprint rows would also be cleared")
        typer.echo("  rerun refresh-nc-redline-fingerprints after detector fixes to verify the slice stays clear")
        return

    result = repository.clear_redline_fingerprint_for_historical_document(
        historical_document_id,
        include_path_rollup=include_path_rollup,
    )
    if result is None:
        typer.echo(f"Historical document not found: {historical_document_id}")
        raise typer.Exit(1)
    typer.echo(
        f"Cleared {result['updated_count']} fingerprint row(s) for hd={historical_document_id} "
        f"pages {result['page_start']}-{result['page_end'] or result['page_start']}"
    )


@lineage_app.command("retire-tariff-version")
def retire_tariff_version(
    version_id: int = typer.Option(..., "--version-id", help="Tariff version id to retire."),
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying the database."),
) -> None:
    """Delete one tariff_version and its charges while leaving the historical document intact."""
    _, repository = _bootstrap()
    if dry_run:
        with repository._connect() as conn:
            row = conn.execute(
                """
                SELECT tv.id, tv.family_key, tv.historical_document_id, tv.effective_start,
                       COUNT(tc.id) AS charge_count
                FROM tariff_versions tv
                LEFT JOIN tariff_charges tc ON tc.version_id = tv.id
                WHERE tv.id = ?
                GROUP BY tv.id, tv.family_key, tv.historical_document_id, tv.effective_start
                """,
                (version_id,),
            ).fetchone()
        if row is None:
            typer.echo(f"Tariff version not found: {version_id}")
            raise typer.Exit(1)
        typer.echo(
            f"[DRY RUN] Would retire version={row['id']} family={row['family_key']} "
            f"historical_document_id={row['historical_document_id'] or '-'} "
            f"effective_start={row['effective_start'] or '-'} "
            f"charges={int(row['charge_count'] or 0)}"
        )
        return

    retired = repository.retire_tariff_version(version_id)
    if retired is None:
        typer.echo(f"Tariff version not found: {version_id}")
        raise typer.Exit(1)
    typer.echo(
        f"Retired version={retired['version_id']} family={retired['family_key']} "
        f"historical_document_id={retired['historical_document_id'] or '-'} "
        f"deleted_charges={retired['deleted_charge_count']}"
    )


@lineage_app.command("deduplicate-tariff-charges")
def deduplicate_tariff_charges(
    version_id: list[int] = typer.Option(..., "--version-id", help="Tariff version id to deduplicate. Repeat for multiple versions."),
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying the database."),
) -> None:
    """Deduplicate repeated tariff_charges rows for one or more versions."""
    _, repository = _bootstrap()
    if dry_run:
        with repository._connect() as conn:
            for vid in version_id:
                before = int(conn.execute("SELECT COUNT(*) FROM tariff_charges WHERE version_id = ?", (vid,)).fetchone()[0])
                unique_count = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM (
                            SELECT 1
                            FROM tariff_charges
                            WHERE version_id = ?
                            GROUP BY
                                charge_type,
                                COALESCE(charge_label, ''),
                                COALESCE(rate_value, -999999999.0),
                                COALESCE(rate_unit, ''),
                                COALESCE(season, ''),
                                COALESCE(tou_period, ''),
                                COALESCE(tier_min, -999999999.0),
                                COALESCE(tier_max, -999999999.0),
                                COALESCE(customer_class, '')
                        )
                        """,
                        (vid,),
                    ).fetchone()[0]
                )
                typer.echo(
                    f"[DRY RUN] version={vid} before={before} unique={unique_count} "
                    f"duplicates_removed={before - unique_count}"
                )
        return

    for vid in version_id:
        result = repository.deduplicate_tariff_charges_for_version(vid)
        typer.echo(
            f"version={result['version_id']} before={result['before_count']} "
            f"after={result['after_count']} duplicates_removed={result['duplicates_removed']}"
        )


@lineage_app.command("retire-provisional-garbage-nc")
def retire_provisional_garbage_nc(
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying DB."),
    state: str = typer.Option("NC", help="State filter (default NC)."),
) -> None:
    """Retire provisional NC families that have no charged tariff content.

    Targets provisional families where every version (if any) has zero charges.
    Families with actual charge rows are always skipped.

    Use --execute to apply the deletions. Default is --dry-run (safe preview).

    Deletes: tariff_families, historical_documents, tariff_versions, tariff_charges,
    historical_processing_runs, historical_reprocess_queue, parse_review_outcomes
    for affected source spans.

    Run export nc-schedule-inventory-audit and show-workflow-status-nc after to confirm.
    """
    _, repository = _bootstrap()
    result = repository.retire_provisional_garbage_families_nc(
        dry_run=dry_run,
        state=state,
    )
    if dry_run:
        typer.echo(
            f"[DRY RUN] Would retire {result['candidates_found']} provisional families "
            f"with no charged content."
        )
        typer.echo("  Re-run with --execute to apply.")
    else:
        typer.echo(f"Retired {result['families_deleted']} provisional families.")
        typer.echo(f"  historical_docs deleted:      {result['historical_docs_deleted']}")
        typer.echo(f"  versions deleted:             {result['versions_deleted']}")
        typer.echo(f"  parse_review rows deleted:    {result['parse_review_rows_deleted']}")
        typer.echo(f"  processing_runs deleted:      {result['processing_runs_deleted']}")
        typer.echo(f"  reprocess_queue rows deleted: {result['reprocess_queue_deleted']}")
        typer.echo("Run: python -m duke_rates show-workflow-status-nc")
        typer.echo("Run: python -m duke_rates export nc-schedule-inventory-audit")


@lineage_app.command("repair-historical-current-snapshot")
def repair_historical_current_snapshot(
    historical_document_id: int = typer.Argument(..., help="Historical document id to repair."),
    requested_by: str = typer.Option("operator", help="Requester label stored on the reprocess queue."),
    queue_priority: int = typer.Option(95, help="Priority for the follow-up reprocess queue item."),
) -> None:
    """Repair a historical row that still points at a stale current-document snapshot."""
    _, repository = _bootstrap()
    repaired = repository.repair_historical_current_document_snapshot(
        historical_document_id,
        requested_by=requested_by,
        queue_priority=queue_priority,
    )
    if repaired is None:
        typer.echo(f"Historical document not found: {historical_document_id}")
        raise typer.Exit(1)
    typer.echo(
        f"Repaired historical document {historical_document_id} -> "
        f"{repaired.family_key} | current_doc={repaired.current_document_id or '-'} | "
        f"{repaired.local_path}"
    )


@lineage_app.command("repair-legacy-ncuc-data")
def repair_legacy_ncuc_data(
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying the database."),
) -> None:
    """Audit and repair legacy NCUC rows that break modern workflow tooling."""
    _, repository = _bootstrap()
    report = repository.repair_legacy_ncuc_data_issues(dry_run=dry_run)

    typer.echo("Legacy NCUC Data Audit")
    typer.echo(f"  legacy_portal_harvest={report['legacy_portal_harvest_count']}")
    typer.echo(
        "  malformed_historical_current_document_id="
        f"{report['malformed_historical_current_document_id_count']}"
    )

    if report["legacy_portal_harvest_rows"]:
        typer.echo("Legacy portal_harvest rows")
        for row in report["legacy_portal_harvest_rows"][:10]:
            typer.echo(
                "  "
                f"id={row['id']} docket={row['docket_number'] or '-'} "
                f"method={row['acquisition_method']} title={(row['filing_title'] or '')[:80]}"
            )

    if report["malformed_historical_current_document_id_rows"]:
        typer.echo("Malformed historical current_document_id rows")
        for row in report["malformed_historical_current_document_id_rows"][:10]:
            typer.echo(
                "  "
                f"id={row['id']} family={row['family_key'] or '-'} "
                f"current_document_id={row['current_document_id']} "
                f"path={(row['local_path'] or '')[:80]}"
            )

    if dry_run:
        typer.echo("Re-run with --execute to normalize these legacy rows.")
        return

    typer.echo(
        "Applied repairs: "
        f"portal_harvest->playwright={report['updated_legacy_portal_harvest_count']} "
        f"cleared_historical_current_document_id={report['cleared_historical_current_document_id_count']}"
    )


@lineage_app.command("attach-current-document-to-family")
def attach_current_document_to_family(
    family_key: str = typer.Argument(..., help="Target tariff family_key."),
    document_id: int = typer.Argument(..., help="Current documents.id to attach."),
) -> None:
    """Attach a current document anchor to an existing tariff family."""
    _, repository = _bootstrap()
    try:
        family = repository.attach_current_document_to_family(
            family_key,
            document_id=document_id,
        )
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    if family is None:
        typer.echo(f"Family not found: {family_key}")
        raise typer.Exit(1)
    typer.echo(
        f"Attached current document {document_id} to {family.family_key} | "
        f"{family.family_type} | {family.title or ''}"
    )


@lineage_app.command("list-current-anchor-mismatches")
def list_current_anchor_mismatches(
    state: str | None = typer.Option(None, help="Filter by state."),
    company: str | None = typer.Option(None, help="Filter by company."),
    family_type: str | None = typer.Option(None, help="Filter by family_type."),
    limit: int = typer.Option(0, help="Max mismatches to show (0 = all)."),
) -> None:
    """List tariff families whose current document anchor contradicts family metadata."""
    _, repository = _bootstrap()
    rows = repository.list_current_anchor_mismatches(
        state=state,
        company=company,
        family_type=family_type,
        limit=limit or None,
    )
    typer.echo(f"Total current-anchor mismatches: {len(rows)}")
    for row in rows:
        typer.echo(
            f"  {row['family_key']:<55} {row['family_schedule_code'] or '?':<14} "
            f"doc={row['current_document_id']:<4} {row['document_schedule_code'] or '?':<14} "
            f"{row['review_action']:<38} "
            f"[{', '.join(row['reasons'])}]"
        )
        typer.echo(
            f"    family: {(row['family_title'] or '')[:80]}"
        )
        typer.echo(
            f"    document: {(row['document_title'] or '')[:80]}"
        )
        if row.get("candidate_leaf_nos"):
            typer.echo(
                f"    mined leafs: {', '.join(row['candidate_leaf_nos'])}"
            )
        if row.get("candidate_headings"):
            typer.echo(
                f"    headings: {', '.join(row['candidate_headings'])}"
            )


@lineage_app.command("sync-family-metadata-from-current-anchor")
def sync_family_metadata_from_current_anchor(
    family_key: str = typer.Argument(..., help="Target tariff family_key."),
) -> None:
    """Sync a family's title/schedule metadata from its anchored current document."""
    _, repository = _bootstrap()
    family = repository.sync_family_metadata_from_current_document(family_key)
    if family is None:
        typer.echo(f"Family not found or has no current document anchor: {family_key}")
        raise typer.Exit(1)
    typer.echo(
        f"Synced {family.family_key} | {family.schedule_code or '?'} | "
        f"{family.tariff_identifier or '?'} | {family.title or ''}"
    )


@lineage_app.command("migrate-historical-family")
def migrate_historical_family_lineage(
    source_family_key: str = typer.Argument(..., help="Source tariff family_key."),
    target_family_key: str = typer.Argument(..., help="Target historical-only family_key."),
    historical_id: list[int] = typer.Option(..., "--historical-id", help="Historical document id to migrate."),
    title: str = typer.Option(..., help="Title for the target historical-only family."),
    schedule_code: str | None = typer.Option(None, help="Schedule code for the target family."),
    family_type: str | None = typer.Option(None, help="Family type for the target family."),
    tariff_identifier: str | None = typer.Option(None, help="Tariff identifier for the target family."),
    alias: list[str] | None = typer.Option(None, "--alias", help="Additional aliases to retain."),
    notes: str | None = typer.Option(None, help="Notes for the target family."),
) -> None:
    """Move selected historical documents into a new historical-only family lineage."""
    _, repository = _bootstrap()
    try:
        family = repository.migrate_historical_family_lineage(
            source_family_key,
            target_family_key,
            historical_document_ids=historical_id,
            title=title,
            schedule_code=schedule_code,
            family_type=family_type,
            tariff_identifier=tariff_identifier,
            aliases=alias,
            notes=notes,
        )
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    if family is None:
        typer.echo(f"Source family not found: {source_family_key}")
        raise typer.Exit(1)
    typer.echo(
        f"Migrated {len(historical_id)} historical docs from {source_family_key} "
        f"to {family.family_key} | {family.schedule_code or '?'} | {family.title or ''}"
    )


@lineage_app.command("canonicalize-historical-family-key")
def canonicalize_historical_family_key(
    source_family_key: str = typer.Argument(..., help="Malformed or legacy source tariff family_key."),
    target_family_key: str = typer.Argument(..., help="Canonical target tariff family_key."),
    historical_id: list[int] | None = typer.Option(None, "--historical-id", help="Optional subset of historical document ids to move."),
    all_historical: bool = typer.Option(False, "--all-historical", help="Move all historical documents currently attached to the source family."),
    title: str | None = typer.Option(None, help="Override title for a newly created target family."),
    schedule_code: str | None = typer.Option(None, help="Override schedule code for a newly created target family."),
    family_type: str | None = typer.Option(None, help="Override family_type for a newly created target family."),
    tariff_identifier: str | None = typer.Option(None, help="Override tariff identifier for a newly created target family."),
    alias: list[str] | None = typer.Option(None, "--alias", help="Additional aliases to retain on the target family."),
    notes: str | None = typer.Option(None, help="Optional notes for the target family."),
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying the database."),
    keep_source_family: bool = typer.Option(False, help="Keep the source family row even if it becomes empty."),
) -> None:
    """Move malformed historical-family lineage into a canonical family key."""
    _, repository = _bootstrap()
    source_family = repository.get_tariff_family(source_family_key)
    if source_family is None:
        typer.echo(f"Source family not found: {source_family_key}")
        raise typer.Exit(1)
    target_family = repository.get_tariff_family(target_family_key)

    if historical_id and all_historical:
        typer.echo("Use either --historical-id or --all-historical, not both.")
        raise typer.Exit(1)
    if not historical_id and not all_historical:
        typer.echo("Specify --all-historical or provide at least one --historical-id.")
        raise typer.Exit(1)
    if target_family is None and not (title or source_family.title):
        typer.echo("A title is required when the target family does not already exist.")
        raise typer.Exit(1)

    selected_ids = historical_id or []
    if all_historical:
        with repository._connect() as conn:
            selected_ids = [
                int(row["id"])
                for row in conn.execute(
                    """
                    SELECT id
                    FROM historical_documents
                    WHERE family_key = ?
                    ORDER BY COALESCE(effective_start, ''), id
                    """,
                    (source_family_key,),
                ).fetchall()
            ]

    if dry_run:
        typer.echo(
            f"[DRY RUN] Would canonicalize {source_family_key} -> {target_family_key}."
        )
        if selected_ids:
            typer.echo(f"  move_historical_ids={','.join(str(item) for item in selected_ids)}")
        else:
            typer.echo("  no source historical docs found; dry run will only repair ancillary/orphan lineage if present.")
        if target_family:
            typer.echo(
                f"  target exists: {target_family.family_key} | "
                f"{target_family.schedule_code or '?'} | {target_family.title or ''}"
            )
        else:
            typer.echo(
                f"  target will be created: {target_family_key} | "
                f"{schedule_code or source_family.schedule_code or '?'} | "
                f"{title or source_family.title or ''}"
            )
        if not keep_source_family:
            typer.echo("  source family will be pruned if it becomes empty.")
        return

    try:
        result = repository.canonicalize_historical_family_key(
            source_family_key,
            target_family_key,
            historical_document_ids=selected_ids,
            title=title,
            schedule_code=schedule_code,
            family_type=family_type,
            tariff_identifier=tariff_identifier,
            aliases=alias,
            notes=notes,
            prune_source_family=not keep_source_family,
        )
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    if result is None:
        typer.echo(f"Source family not found: {source_family_key}")
        raise typer.Exit(1)

    family = result["family"]
    typer.echo(
        f"Canonicalized {len(result['moved_historical_document_ids'])} historical docs from {source_family_key} "
        f"to {family.family_key} | {family.schedule_code or '?'} | {family.title or ''}"
    )
    typer.echo(f"  source_family_pruned={result['source_family_pruned']}")


@lineage_app.command("canonicalize-doc-families-nc")
def canonicalize_doc_families_nc(
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying DB."),
    limit: int = typer.Option(0, "--limit", help="Max families to canonicalize (0 = all)."),
) -> None:
    """Scan remaining doc-* families and canonicalize them to schedule/rider keys.

    Infers the correct canonical family key from document titles and content.
    Supports bulk --execute to promote all eligible doc-* families at once.
    """
    _, repository = _bootstrap()
    conn = connect_sqlite(repository.database_path)
    try:
        rows = conn.execute(
            """
            SELECT tf.family_key, tf.title, tf.state, tf.company,
                   COUNT(DISTINCT hd.id) AS doc_count,
                   SUM((SELECT COUNT(*) FROM tariff_charges tc
                        JOIN tariff_versions tv ON tv.id = tc.version_id
                        WHERE tv.family_key = tf.family_key)) AS charge_count
            FROM tariff_families tf
            LEFT JOIN historical_documents hd ON hd.family_key = tf.family_key
            WHERE tf.family_key LIKE 'nc-%doc-%'
            GROUP BY tf.family_key
            ORDER BY charge_count DESC, doc_count DESC
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        typer.echo("No doc-* families found. Nothing to canonicalize.")
        return

    typer.echo(f"Found {len(rows)} doc-* families.\n")

    actions: list[dict] = []
    for row in rows:
        fk = row["family_key"]
        title = row["title"] or ""
        company = row["company"] or ""

        # Infer canonical key from title
        canonical = _infer_canonical_family_key(fk, title, company)
        actions.append({
            "family_key": fk,
            "canonical_key": canonical,
            "title": title,
            "company": company,
            "doc_count": row["doc_count"],
            "charge_count": row["charge_count"],
        })

    for i, a in enumerate(actions):
        if limit and i >= limit:
            break
        typer.echo(
            f"  {a['family_key']}"
            f"\n    -&gt; {a['canonical_key']}"
            f"\n    title={_safe_cli_text(a['title'][:80])}  "
            f"docs={a['doc_count']}  charges={a['charge_count']}"
        )

    if dry_run:
        typer.echo(f"\n[DRY RUN] Would canonicalize {min(len(actions), limit) if limit else len(actions)} families.")
        typer.echo("Re-run with --execute to apply.")
        return

    migrated = 0
    for a in actions:
        if limit and migrated >= limit:
            break
        try:
            conn2 = connect_sqlite(repository.database_path)
            try:
                _apply_canonicalization(conn2, a["family_key"], a["canonical_key"])
                conn2.commit()
                migrated += 1
                typer.echo(f"  OK  {a['family_key']} -&gt; {a['canonical_key']}")
            finally:
                conn2.close()
        except Exception as exc:
            typer.echo(f"  FAIL  {a['family_key']}: {exc}")

    typer.echo(f"\nCanonicalized {migrated} families.")


def _infer_canonical_family_key(family_key: str, title: str, company: str) -> str:
    """Infer a canonical family key from document title and existing key."""
    import re

    title_lower = title.lower()
    key_lower = family_key.lower()

    # Extract leaf number if present
    leaf_match = re.search(r"leaf\s*(?:no\.?\s*)?(\d{1,4})", title_lower)
    leaf_no = leaf_match.group(1) if leaf_match else None

    company_prefix = "nc-progress" if "progress" in key_lower else (
        "nc-carolinas" if "carolinas" in key_lower else "nc"
    )

    # Detect schedule patterns
    schedule_patterns = [
        (r"schedule\s+rs\b", f"{company_prefix}-schedule-rs"),
        (r"schedule\s+re\b", f"{company_prefix}-schedule-re"),
        (r"schedule\s+r[-\s]?tou", f"{company_prefix}-schedule-r-tou"),
        (r"schedule\s+r[-\s]?toud?\b", f"{company_prefix}-schedule-r-toud"),
        (r"schedule\s+res\b", f"{company_prefix}-schedule-res"),
        (r"schedule\s+sgs[-\s]?toue?\b", f"{company_prefix}-schedule-sgs-toue"),
        (r"schedule\s+sgs\b", f"{company_prefix}-schedule-sgs"),
        (r"schedule\s+lgs[-\s]?toue?\b", f"{company_prefix}-schedule-lgs-toue"),
        (r"schedule\s+lgs\b", f"{company_prefix}-schedule-lgs"),
        (r"schedule\s+pg\b", f"{company_prefix}-schedule-pg"),
        (r"schedule\s+ts\b", f"{company_prefix}-schedule-ts"),
        (r"schedule\s+hlf\b", f"{company_prefix}-schedule-hlf"),
        (r"schedule\s+i\b", f"{company_prefix}-schedule-i"),
        (r"schedule\s+fl\b", f"{company_prefix}-schedule-fl"),
        (r"schedule\s+wc\b", f"{company_prefix}-schedule-wc"),
        (r"schedule\s+nm\b", f"{company_prefix}-schedule-nm"),
        (r"schedule\s+ol\b", f"{company_prefix}-schedule-ol"),
        (r"schedule\s+se\b", f"{company_prefix}-schedule-se"),
        (r"schedule\s+lp\b", f"{company_prefix}-schedule-lp"),
        (r"schedule\s+isl?\b", f"{company_prefix}-schedule-is"),
        (r"schedule\s+dsm\b", f"{company_prefix}-schedule-dsm"),
        (r"schedule\s+ee\b", f"{company_prefix}-schedule-ee"),
        (r"schedule\s+opt[-\s]?e\b", f"{company_prefix}-schedule-opte"),
        (r"schedule\s+opt[-\s]?h\b", f"{company_prefix}-schedule-opth"),
        (r"schedule\s+opt[-\s]?g\b", f"{company_prefix}-schedule-optg"),
        (r"schedule\s+cpp\b", f"{company_prefix}-schedule-cpp"),
        (r"schedule\s+fcar\b", f"{company_prefix}-schedule-fcar"),
        (r"schedule\s+edpr\b", f"{company_prefix}-schedule-edpr"),
        (r"schedule\s+sbes\b", f"{company_prefix}-schedule-sbes"),
        (r"schedule\s+iqheu\b", f"{company_prefix}-schedule-iqheu"),
        (r"schedule\s+gs\b", f"{company_prefix}-schedule-gs"),
    ]

    for pattern, canonical in schedule_patterns:
        if re.search(pattern, title_lower):
            if leaf_no:
                return f"{canonical}-leaf-{leaf_no}"
            return canonical

    # Detect rider patterns
    rider_match = re.search(r"rider\s+(\w[\w\s-]{0,20})", title_lower)
    if rider_match:
        rider_code = rider_match.group(1).strip().upper().replace(" ", "-")[:15]
        return f"{company_prefix}-rider-{rider_code}"

    # Fallback: extract meaningful part from doc-* key
    doc_part = re.sub(r"^nc-(?:progress|carolinas)-doc-", "", family_key, flags=re.IGNORECASE)
    doc_part = re.sub(r"[^a-zA-Z0-9_-]", "", doc_part)[:60].strip("-").lower()
    if doc_part:
        if leaf_no:
            return f"{company_prefix}-schedule-{doc_part}-leaf-{leaf_no}"
        return f"{company_prefix}-schedule-{doc_part}"

    return family_key  # No inference possible


def _apply_canonicalization(conn: sqlite3.Connection, old_key: str, new_key: str) -> None:
    """Migrate all rows from old family key to new key, creating target family if needed."""
    target = conn.execute(
        "SELECT family_key FROM tariff_families WHERE family_key = ?", (new_key,)
    ).fetchone()

    if not target:
        # Create the canonical family with metadata from the old one
        old = conn.execute(
            "SELECT state, company, title, category FROM tariff_families WHERE family_key = ?",
            (old_key,),
        ).fetchone()
        if old:
            conn.execute(
                """INSERT INTO tariff_families (family_key, state, company, title, category, is_provisional, is_curated)
                   VALUES (?, ?, ?, ?, ?, 0, 1)""",
                (new_key, old["state"], old["company"], old["title"], old["category"]),
            )

    # Update all referencing tables
    for table, col in [
        ("historical_documents", "family_key"),
        ("tariff_versions", "family_key"),
        ("tariff_families", "family_key"),
        ("historical_reprocess_queue", "family_key"),
        ("ncuc_discovery_records", "family_key"),
        ("ncuc_missing_doc_targets", "family_key"),
    ]:
        try:
            conn.execute(
                f"UPDATE {table} SET {col} = ? WHERE {col} = ?",
                (new_key, old_key),
            )
        except sqlite3.OperationalError:
            pass  # Column may not exist in some tables

    # Update historical_processing_runs via historical_documents
    conn.execute(
        """UPDATE historical_processing_runs SET family_key = ?
           WHERE historical_document_id IN (
               SELECT id FROM historical_documents WHERE family_key = ?
           )""",
        (new_key, new_key),
    )

    # Delete old family if it was a doc-* key (and different from new)
    if old_key != new_key and "doc-" in old_key.lower():
        conn.execute("DELETE FROM tariff_families WHERE family_key = ?", (old_key,))


@lineage_app.command("deduplicate-family-nc")
def deduplicate_family_nc(
    family_key: str = typer.Option(..., "--family-key", help="Family key to deduplicate."),
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying DB."),
) -> None:
    """Deduplicate charges across all versions in a tariff family.

    Uses the natural charge signature (type, label, rate, unit, season,
    tou_period, tier, customer_class) to find and remove duplicates.
    """
    _, repository = _bootstrap()
    conn = connect_sqlite(repository.database_path)
    try:
        version_ids = [
            row[0] for row in conn.execute(
                "SELECT id FROM tariff_versions WHERE family_key = ?",
                (family_key,),
            ).fetchall()
        ]
    finally:
        conn.close()

    if not version_ids:
        typer.echo(f"No versions found for family {family_key}.")
        return

    total_before = 0
    total_unique = 0

    for vid in version_ids:
        conn = connect_sqlite(repository.database_path)
        try:
            before = int(conn.execute(
                "SELECT COUNT(*) FROM tariff_charges WHERE version_id = ?", (vid,)
            ).fetchone()[0])
            unique_count = int(conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT 1 FROM tariff_charges
                    WHERE version_id = ?
                    GROUP BY
                        charge_type,
                        COALESCE(charge_label, ''),
                        COALESCE(rate_value, -999999999.0),
                        COALESCE(rate_unit, ''),
                        COALESCE(season, ''),
                        COALESCE(tou_period, ''),
                        COALESCE(tier_min, -999999999.0),
                        COALESCE(tier_max, -999999999.0),
                        COALESCE(customer_class, '')
                )
                """,
                (vid,),
            ).fetchone()[0]
            )
            total_before += before
            total_unique += unique_count

            if not dry_run and before > unique_count:
                repository.deduplicate_tariff_charges_for_version(vid)

            if before != unique_count:
                typer.echo(
                    f"  {'[DRY RUN]' if dry_run else '[EXECUTED]'} "
                    f"version={vid} before={before} unique={unique_count} "
                    f"duplicates={before - unique_count}"
                )
        finally:
            conn.close()

    dup_count = total_before - total_unique
    typer.echo(
        f"\nFamily {family_key}: {len(version_ids)} versions, "
        f"{total_before} charges -&gt; {total_unique} unique "
        f"({dup_count} duplicates {'(dry run)' if dry_run else 'removed'})"
    )


@lineage_app.command("deduplicate-documents-nc")
def deduplicate_documents_nc(
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying DB (default: dry-run)."),
    file_hash: str = typer.Option("", "--file-hash", help="Target a specific content_hash group only."),
    limit: int = typer.Option(0, "--limit", help="Max duplicate groups to process (0 = all)."),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Consolidate historical documents that share the same content_hash.

    For each group of duplicates the best survivor is kept (most charges,
    has local_path, newest retrieved_at) and all foreign-key references
    from the other rows are remapped to it.

    Always preview with --dry-run first.
    """
    _, repository = _bootstrap()

    result = repository.deduplicate_historical_documents(
        dry_run=dry_run,
        file_hash=file_hash if file_hash else None,
        limit=limit,
    )

    if json_out:
        typer.echo(json.dumps(result, indent=2, default=str))
        return

    typer.echo(
        f"\nDocument Deduplication {'(DRY RUN)' if dry_run else '(EXECUTED)'}"
    )
    typer.echo(f"  Total duplicate groups: {result['total_groups']}")
    typer.echo(f"  Groups processed:      {result['groups_processed']}")
    typer.echo(f"  Documents to remove:   {result['documents_removed']}")

    if result["errors"]:
        typer.echo(f"\n  Errors ({len(result['errors'])}):")
        for e in result["errors"]:
            typer.echo(f"    - {e}")

    for pg in result["per_group"]:
        typer.echo(
            f"\n  content_hash={pg['content_hash'][:16]}... "
            f"survivor=hd:{pg['survivor_id']} ({pg['survivor_charges']} charges) "
            f"remove={pg['group_size'] - 1} docs [hd:{', hd:'.join(str(i) for i in pg['removed_ids'])}]"
        )

    typer.echo(
        f"\n  Duration: {result['duration_ms']}ms"
    )

    if dry_run and result["documents_removed"] > 0:
        typer.echo("\n  Re-run with --execute to apply changes.")


@lineage_app.command("backfill-evidence-nc")
def backfill_evidence_nc(
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying DB (default: dry-run)."),
    limit: int = typer.Option(0, "--limit", help="Max documents to backfill (0 = all candidates)."),
    family: str = typer.Option("", "--family", help="Target a specific family key."),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Backfill evidence_json for historical documents where it is missing.

    Extracts the best-family evidence breakdown from existing span artifacts
    (ncuc_span_artifacts). Documents without span artifacts are skipped —
    they need full reprocessing via reprocess enqueue-stale-nc.
    """
    _, repository = _bootstrap()

    result = repository.backfill_evidence_json(
        dry_run=dry_run,
        limit=limit,
        family_key=family if family else None,
    )

    if json_out:
        typer.echo(json.dumps(result, indent=2, default=str))
        return

    typer.echo(
        f"\nEvidence Backfill {'(DRY RUN)' if dry_run else '(EXECUTED)'}"
    )
    typer.echo(f"  Total candidates:       {result['total_candidates']}")
    typer.echo(f"  Would backfill:         {result['backfilled']}")
    typer.echo(f"  Skipped (no spans):     {result['skipped_no_spans']}")
    typer.echo(f"  Skipped (no breakdown): {result['skipped_no_breakdown']}")

    if result["errors"]:
        typer.echo(f"\n  Errors ({len(result['errors'])}):")
        for e in result["errors"][:10]:
            typer.echo(f"    - {e}")

    if result["backfilled"] > 0 and not json_out:
        typer.echo(f"\n  Top backfills:")
        for d in result["per_doc"][:5]:
            score = d.get("evidence_score", "?")
            typer.echo(
                f"    hd:{d['historical_document_id']} "
                f"family={d['family_key']} "
                f"score={score}"
            )

    typer.echo(f"\n  Duration: {result['duration_ms']}ms")

    if dry_run and result["backfilled"] > 0:
        typer.echo("\n  Re-run with --execute to apply changes.")
    if result["skipped_no_spans"] > 0:
        typer.echo(
            f"\n  {result['skipped_no_spans']} docs have no span artifacts. "
            f"Use 'reprocess enqueue-stale-nc' for full regeneration."
        )


@lineage_app.command("backfill-content-hash-nc")
def backfill_content_hash_nc(
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying DB (default: dry-run)."),
    limit: int = typer.Option(0, "--limit", help="Max documents to hash (0 = all)."),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Backfill content_hash for historical_documents where it is null or empty.

    Calculates SHA-1 checksums from the files on disk and writes them to the
    content_hash column.  Documents whose files are missing are skipped.

    This is a prerequisite for span-artifact matching and evidence backfill.
    """
    import hashlib as _hashlib
    from pathlib import Path as _Path

    _, repository = _bootstrap()
    conn = connect_sqlite(repository.database_path)
    try:
        candidates = conn.execute(
            """
            SELECT id, local_path FROM historical_documents
            WHERE local_path IS NOT NULL AND local_path != ''
              AND (content_hash IS NULL OR content_hash = '')
            ORDER BY id
            """
            + (" LIMIT ?" if limit > 0 else ""),
            (limit,) if limit > 0 else (),
        ).fetchall()

        hashed = 0
        skipped_missing = 0
        errors: list[str] = []

        for c in candidates:
            hd_id = c["id"]
            local_path = c["local_path"]
            file_path = _Path(local_path)
            if not file_path.exists():
                skipped_missing += 1
                continue

            if dry_run:
                hashed += 1
                continue

            try:
                sha1 = _hashlib.sha1()
                with open(file_path, "rb") as fh:
                    while True:
                        chunk = fh.read(65536)
                        if not chunk:
                            break
                        sha1.update(chunk)
                ch = sha1.hexdigest()
                conn.execute(
                    "UPDATE historical_documents SET content_hash = ? WHERE id = ?",
                    (ch, hd_id),
                )
                hashed += 1
            except Exception as exc:
                errors.append(f"hd:{hd_id}: {exc}")

        if not dry_run and hashed > 0:
            conn.commit()

        result = {
            "dry_run": dry_run,
            "total_candidates": len(candidates),
            "hashed": hashed,
            "skipped_missing": skipped_missing,
            "errors": errors,
        }
    finally:
        conn.close()

    if json_out:
        typer.echo(json.dumps(result, indent=2, default=str))
        return

    typer.echo(
        f"\nContent Hash Backfill {'(DRY RUN)' if dry_run else '(EXECUTED)'}"
    )
    typer.echo(f"  Total candidates:       {result['total_candidates']}")
    typer.echo(f"  Would hash:             {result['hashed']}")
    typer.echo(f"  Skipped (file missing): {result['skipped_missing']}")

    if result["errors"]:
        typer.echo(f"\n  Errors ({len(result['errors'])}):")
        for e in result["errors"][:10]:
            typer.echo(f"    - {e}")

    if dry_run and result["hashed"] > 0:
        typer.echo("\n  Re-run with --execute to apply changes.")


@lineage_app.command("repair-anomaly-nc")
def repair_anomaly_nc(
    historical_document_id: int = typer.Option(
        ..., "--hd-id", help="Historical document ID to repair."
    ),
    repair_action: str = typer.Option(
        "", "--action", help="Repair action: rebind_span, reassign_profile, enqueue_ocr, accept_caveat, or auto-detect if empty."
    ),
    new_start_page: int = typer.Option(None, "--start-page", help="New start page for rebind_span."),
    new_end_page: int = typer.Option(None, "--end-page", help="New end page for rebind_span."),
    new_profile: str = typer.Option(None, "--profile", help="Target profile for reassign_profile."),
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview without modifying DB."),
) -> None:
    """Apply a known repair pattern to an anomalous document.

    Supported actions:
      rebind_span    -- Update start_page/end_page on historical_documents
      reassign_profile -- Re-queue the doc with a different profile hint
      enqueue_ocr    -- Enqueue the doc in the OCR queue
      accept_caveat  -- Mark as accepted caveat (no DB change, just log)
      auto-detect    -- Detect the best action from the document's current state
    """
    from duke_rates.db.reprocess import latest_processing_run_for_document

    settings, _ = _bootstrap()
    conn = connect_sqlite(settings.database_path)
    try:
        doc = conn.execute(
            "SELECT * FROM historical_documents WHERE id = ?",
            (historical_document_id,),
        ).fetchone()
        if not doc:
            raise typer.BadParameter(f"Historical document {historical_document_id} not found.")

        run = latest_processing_run_for_document(conn, historical_document_id=historical_document_id)

        typer.echo(f"Document hd={historical_document_id}  family={doc['family_key']}")
        typer.echo(f"  effective_start={doc['effective_start']}  "
                   f"pages={doc['start_page']}-{doc['end_page']}")
        typer.echo(f"  title={_safe_cli_text(doc['title'] or '-')}")

        if run:
            metadata = json.loads(run["metadata_json"] or "{}")
            signals = metadata.get("signals") or {}
            text_len = (metadata.get("text_metrics") or {}).get("text_length") or 0
            typer.echo(f"  profile={run['parser_profile']}  status={run['status']}  "
                       f"quality={run['outcome_quality']}  charges={run['charge_count']}  "
                       f"text_len={text_len}")
            typer.echo(f"  has_carolinas_text={signals.get('has_carolinas_company_text')}  "
                       f"has_progress_text={signals.get('has_progress_company_text')}")

        # Auto-detect action
        if not repair_action:
            repair_action = _detect_anomaly_repair(dict(doc), run)

        typer.echo(f"\n  Action: {repair_action}")

        if repair_action == "accept_caveat":
            typer.echo(f"  No DB changes. This anomaly is an accepted caveat.")
            typer.echo(f"  Reason: structural parser limitation or non-tariff content.")
            if not dry_run:
                typer.echo(f"  [EXECUTED] Caveat accepted.")
            return

        if repair_action == "rebind_span":
            if new_start_page is None or new_end_page is None:
                raise typer.BadParameter("--start-page and --end-page required for rebind_span.")
            typer.echo(f"  New pages: {new_start_page}-{new_end_page}")
            if not dry_run:
                conn.execute(
                    "UPDATE historical_documents SET start_page=?, end_page=?, "
                    "title=? WHERE id=?",
                    (
                        new_start_page, new_end_page,
                        f"{doc['title'] or 'Untitled'} (Span {new_start_page}-{new_end_page})",
                        historical_document_id,
                    ),
                )
                conn.commit()
                typer.echo(f"  [EXECUTED] Span updated. Re-queue with:")
                typer.echo(f"    python -m duke_rates reprocess enqueue-nc --hd-id {historical_document_id} --priority 90")
            else:
                typer.echo(f"  [DRY RUN] Would update span to {new_start_page}-{new_end_page}")

        elif repair_action == "reassign_profile":
            if not new_profile:
                raise typer.BadParameter("--profile required for reassign_profile.")
            typer.echo(f"  Target profile: {new_profile}")
            if not dry_run:
                # Re-queue with metadata hint
                conn.execute(
                    """INSERT INTO historical_reprocess_queue
                       (historical_document_id, source_pdf, family_key, priority,
                        queue_reason, requested_by, requested_at)
                       VALUES (?, ?, ?, 90, ?, 'repair_anomaly', datetime('now'))""",
                    (
                        historical_document_id,
                        doc["local_path"],
                        doc["family_key"],
                        f"profile_reassign:{run['parser_profile']}->{new_profile}",
                    ),
                )
                conn.commit()
                typer.echo(f"  [EXECUTED] Re-queued with profile hint. Process with:")
                typer.echo(f"    python -m duke_rates reprocess process-queue-nc")
            else:
                typer.echo(f"  [DRY RUN] Would re-queue with profile={new_profile}")

        elif repair_action == "enqueue_ocr":
            if not dry_run:
                conn.execute(
                    """INSERT INTO ocr_processing_queue
                       (historical_document_id, source_pdf, family_key, status,
                        ocr_backend, priority, requested_by, requested_at)
                       VALUES (?, ?, ?, 'pending', 'pytesseract_cpu', 90,
                               'repair_anomaly', datetime('now'))""",
                    (
                        historical_document_id,
                        doc["local_path"],
                        doc["family_key"],
                    ),
                )
                conn.commit()
                typer.echo(f"  [EXECUTED] Enqueued in OCR queue. Process with:")
                typer.echo(f"    python -m duke_rates ocr process-queue-nc")
            else:
                typer.echo(f"  [DRY RUN] Would enqueue in OCR queue")

        else:
            raise typer.BadParameter(f"Unknown repair action: {repair_action}")

    finally:
        conn.close()


