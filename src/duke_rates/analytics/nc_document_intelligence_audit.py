from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

try:
    import pdfplumber
except ImportError:  # pragma: no cover
    pdfplumber = None

from duke_rates.db.artifact_cache import load_page_artifacts
from duke_rates.document_intelligence.extraction import SchemaExtractionAdapter
from duke_rates.document_intelligence.fingerprinting import HybridDocumentFingerprinter
from duke_rates.document_intelligence.models import DocumentConfidence
from duke_rates.document_intelligence.representation import DocumentRepresentationBuilder
from duke_rates.document_intelligence.validation import ExtractionValidationEngine

_DEFAULT_OUTPUT_DIR = Path("docs/reports/nc_document_intelligence_audit")


@dataclass(frozen=True)
class AuditCandidate:
    historical_document_id: int
    version_id: int
    family_key: str
    company: str | None
    title: str | None
    local_path: str | None
    content_hash: str | None
    effective_start: str | None
    start_page: int | None
    end_page: int | None
    charge_count: int
    latest_run_status: str | None
    latest_outcome_quality: str | None
    latest_parser_profile: str | None


def build_nc_document_intelligence_audit(
    database_path: Path | None = None,
    *,
    limit: int = 150,
) -> dict[str, object]:
    db_path = Path(database_path or "data/db/duke_rates.db")
    representation_builder = DocumentRepresentationBuilder()
    fingerprinter = HybridDocumentFingerprinter()
    schema_adapter = SchemaExtractionAdapter()
    validator = ExtractionValidationEngine()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        candidates = _load_candidates(conn)[:limit]
        rows: list[dict[str, Any]] = []
        for candidate in candidates:
            page_artifacts = _load_page_artifact_dicts(conn, candidate)
            raw_text = _extract_bounded_text(candidate)
            if not raw_text and page_artifacts:
                raw_text = "\n\n".join(str(item.get("text_content") or "") for item in page_artifacts)
            if not raw_text:
                raw_text = str(candidate.title or "")

            representation = representation_builder.build_historical_document(
                {
                    "id": candidate.historical_document_id,
                    "family_key": candidate.family_key,
                    "company": candidate.company,
                    "state": "NC",
                    "title": candidate.title,
                    "local_path": candidate.local_path,
                    "content_hash": candidate.content_hash,
                    "effective_start": candidate.effective_start,
                    "start_page": candidate.start_page,
                    "end_page": candidate.end_page,
                },
                raw_text=raw_text,
                page_artifacts=page_artifacts,
            )
            fingerprint = fingerprinter.fingerprint(representation)
            extraction = schema_adapter.build_extraction_result(
                representation,
                fingerprint,
                parser_profile=candidate.latest_parser_profile,
                charge_count=candidate.charge_count,
                status=candidate.latest_run_status or ("parsed" if candidate.charge_count else "empty"),
            )
            validation = validator.validate(representation, extraction)
            extraction.validation_passed = validation.passed
            validation_confidence = 1.0 if validation.passed and not validation.warnings else 0.7 if validation.passed else 0.35
            confidence = DocumentConfidence(
                classification_confidence=fingerprint.confidence,
                extraction_confidence=extraction.confidence,
                validation_confidence=validation_confidence,
                overall_confidence=round(
                    (fingerprint.confidence * 0.3)
                    + (extraction.confidence * 0.4)
                    + (validation_confidence * 0.3),
                    4,
                ),
            )
            rows.append(
                {
                    "historical_document_id": candidate.historical_document_id,
                    "version_id": candidate.version_id,
                    "family_key": candidate.family_key,
                    "company": candidate.company,
                    "title": candidate.title,
                    "effective_start": candidate.effective_start,
                    "charge_count": candidate.charge_count,
                    "latest_run_status": candidate.latest_run_status,
                    "latest_outcome_quality": candidate.latest_outcome_quality,
                    "latest_parser_profile": candidate.latest_parser_profile,
                    "doc_type": fingerprint.doc_type.value,
                    "subtype": fingerprint.subtype,
                    "parse_lane": fingerprint.parse_lane.value,
                    "schema_type": extraction.schema_type.value,
                    "schema_family_key": extraction.data.get("family_key"),
                    "schema_schedule_code": extraction.data.get("schedule_code"),
                    "schema_rider_code": extraction.data.get("rider_code"),
                    "validation_passed": validation.passed,
                    "warning_count": len(validation.warnings),
                    "error_count": len(validation.errors),
                    "overall_confidence": confidence.overall_confidence,
                    "features_detected": ", ".join(fingerprint.features_detected),
                    "recommended_action": _recommended_action(candidate, fingerprint, validation),
                    "reason": _reason(candidate, fingerprint, validation),
                }
            )
    finally:
        conn.close()

    rows.sort(
        key=lambda item: (
            item["recommended_action"] != "likely_ok",
            item["warning_count"],
            item["charge_count"] == 0,
            item["overall_confidence"],
        ),
        reverse=True,
    )

    action_counts: dict[str, int] = {}
    for row in rows:
        action = str(row["recommended_action"])
        action_counts[action] = action_counts.get(action, 0) + 1

    return {
        "generated_at": date.today().isoformat(),
        "candidate_count": len(rows),
        "action_counts": dict(sorted(action_counts.items())),
        "rows": rows,
    }


def export_nc_document_intelligence_audit(
    output_dir: Path,
    *,
    database_path: Path | None = None,
    limit: int = 150,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_nc_document_intelligence_audit(database_path, limit=limit)

    rows_csv = output_dir / "nc_document_intelligence_audit_rows.csv"
    summary_json = output_dir / "nc_document_intelligence_audit_summary.json"
    markdown = output_dir / "nc_document_intelligence_audit.md"

    _write_csv(rows_csv, report["rows"])
    summary_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown.write_text(_render_markdown(report), encoding="utf-8")
    return {
        "rows_csv": rows_csv,
        "summary_json": summary_json,
        "markdown": markdown,
    }


def _load_candidates(conn: sqlite3.Connection) -> list[AuditCandidate]:
    rows = conn.execute(
        """
        WITH latest_runs AS (
            SELECT hpr.*
            FROM historical_processing_runs hpr
            INNER JOIN (
                SELECT historical_document_id, MAX(id) AS max_id
                FROM historical_processing_runs
                GROUP BY historical_document_id
            ) latest
              ON latest.max_id = hpr.id
        )
        SELECT
            hd.id AS historical_document_id,
            tv.id AS version_id,
            hd.family_key,
            hd.company,
            hd.title,
            hd.local_path,
            hd.content_hash,
            hd.effective_start,
            hd.start_page,
            hd.end_page,
            COALESCE(vcs.charge_count, 0) AS charge_count,
            lr.status AS latest_run_status,
            lr.outcome_quality AS latest_outcome_quality,
            lr.parser_profile AS latest_parser_profile
        FROM historical_documents hd
        INNER JOIN tariff_versions tv
          ON tv.historical_document_id = hd.id
        LEFT JOIN v_version_charge_summary vcs
          ON vcs.version_id = tv.id
        LEFT JOIN latest_runs lr
          ON lr.historical_document_id = hd.id
        WHERE hd.state = 'NC'
          AND hd.company IN ('progress', 'carolinas')
          AND hd.local_path IS NOT NULL
          AND (
            COALESCE(vcs.charge_count, 0) = 0
            OR hd.family_key LIKE 'nc-%-doc-%'
          )
        ORDER BY
          CASE WHEN hd.family_key LIKE 'nc-%-doc-%' THEN 0 ELSE 1 END,
          COALESCE(vcs.charge_count, 0),
          hd.company,
          hd.family_key,
          COALESCE(hd.effective_start, '')
        """
    ).fetchall()
    return [
        AuditCandidate(
            historical_document_id=int(row["historical_document_id"]),
            version_id=int(row["version_id"]),
            family_key=str(row["family_key"]),
            company=row["company"],
            title=row["title"],
            local_path=row["local_path"],
            content_hash=row["content_hash"],
            effective_start=row["effective_start"],
            start_page=row["start_page"],
            end_page=row["end_page"],
            charge_count=int(row["charge_count"] or 0),
            latest_run_status=row["latest_run_status"],
            latest_outcome_quality=row["latest_outcome_quality"],
            latest_parser_profile=row["latest_parser_profile"],
        )
        for row in rows
    ]


def _load_page_artifact_dicts(
    conn: sqlite3.Connection,
    candidate: AuditCandidate,
) -> list[dict[str, Any]]:
    pages = load_page_artifacts(
        conn,
        source_pdf=str(candidate.local_path or ""),
        file_hash=candidate.content_hash,
    )
    return [page.model_dump(mode="json") for page in pages]


def _extract_bounded_text(candidate: AuditCandidate) -> str:
    path = Path(candidate.local_path or "")
    if not path.exists() or pdfplumber is None:
        return ""
    try:
        with pdfplumber.open(path) as pdf:
            pages = pdf.pages
            if candidate.start_page and candidate.end_page:
                pages = pages[max(candidate.start_page - 1, 0): candidate.end_page]
            elif candidate.start_page:
                start_index = max(candidate.start_page - 1, 0)
                pages = pages[start_index:start_index + 5]
            text_parts: list[str] = []
            for page in pages:
                text = page.extract_text()
                if text:
                    text_parts.append(text)
            return "\n\n".join(text_parts)
    except Exception:
        return ""


def _recommended_action(candidate: AuditCandidate, fingerprint: Any, validation: Any) -> str:
    family_key = candidate.family_key
    doc_type = fingerprint.doc_type.value
    if "-doc-" in family_key and doc_type in {"tariff_sheet", "rider"}:
        return "canonicalize_family_key"
    if candidate.charge_count == 0 and doc_type in {"tariff_sheet", "rider"}:
        return "inspect_and_reparse"
    if doc_type in {"commission_order", "testimony", "correspondence"}:
        return "retire_or_reclassify"
    if not validation.passed or validation.warnings:
        return "review_validation_warnings"
    return "likely_ok"


def _reason(candidate: AuditCandidate, fingerprint: Any, validation: Any) -> str:
    if "-doc-" in candidate.family_key and fingerprint.doc_type.value in {"tariff_sheet", "rider"}:
        return "Malformed doc-* family key appears to contain real tariff/rider content."
    if candidate.charge_count == 0 and fingerprint.doc_type.value in {"tariff_sheet", "rider"}:
        return "Zero-charge historical row still fingerprints as tariff/rider content."
    if fingerprint.doc_type.value in {"commission_order", "testimony", "correspondence"}:
        return "Document-intelligence classification suggests non-billable regulatory content."
    if validation.warnings:
        return validation.warnings[0].message
    return "Document-intelligence signals do not currently indicate a high-priority mismatch."


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "historical_document_id",
        "version_id",
        "family_key",
        "company",
        "title",
        "effective_start",
        "charge_count",
        "latest_run_status",
        "latest_outcome_quality",
        "latest_parser_profile",
        "doc_type",
        "subtype",
        "parse_lane",
        "schema_type",
        "schema_family_key",
        "schema_schedule_code",
        "schema_rider_code",
        "validation_passed",
        "warning_count",
        "error_count",
        "overall_confidence",
        "features_detected",
        "recommended_action",
        "reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _render_markdown(report: dict[str, object]) -> str:
    rows = list(report["rows"])
    lines = [
        "# NC Document Intelligence Audit",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "This report applies the new document-intelligence layer to NC historical rows that are either:",
        "- zero-charge linked tariff versions",
        "- legacy malformed `doc-*` historical families",
        "",
        "It is intended to drive DEC historical canonicalization and malformed-family cleanup.",
        "",
        "## Summary",
        "",
        f"- Candidates analyzed: {report['candidate_count']}",
        "- Action counts:",
    ]
    for action, count in dict(report["action_counts"]).items():
        lines.append(f"  - `{action}`: {count}")
    lines.extend(
        [
            "",
            "## Top Candidates",
            "",
            "| hd | version | family_key | doc_type | lane | charges | action | reason |",
            "|---|---:|---|---|---|---:|---|---|",
        ]
    )
    for row in rows[:40]:
        lines.append(
            f"| {row['historical_document_id']} | {row['version_id']} | `{row['family_key']}` | "
            f"`{row['doc_type']}` | `{row['parse_lane']}` | {row['charge_count']} | "
            f"`{row['recommended_action']}` | {row['reason']} |"
        )
    if len(rows) > 40:
        lines.extend(["", f"Only the top 40 rows are shown here; see CSV for all {len(rows)} rows."])
    lines.append("")
    return "\n".join(lines)
