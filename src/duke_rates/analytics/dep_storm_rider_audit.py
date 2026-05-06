from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

_DEFAULT_OUTPUT_DIR = Path("docs/reports/dep_storm_rider_audit")
_FAMILY_HINTS: dict[str, dict[str, str]] = {
    "nc-progress-leaf-607": {
        "rider_label": "STS-607",
        "search_hint": 'DEP storm rider | "Leaf No. 607" | "Storm Recovery Rider"',
        "docket_hint": "Storm rider / current tariff leaf",
    },
    "nc-progress-leaf-613": {
        "rider_label": "STS-613",
        "search_hint": 'DEP storm rider | "Leaf No. 613" | "Storm Securitization Rider"',
        "docket_hint": "Storm rider / current tariff leaf",
    },
    "nc-progress-doc-STORMRECOVERYRIDER": {
        "rider_label": "doc-STORMRECOVERYRIDER",
        "search_hint": 'DEP storm rider legacy doc family',
        "docket_hint": "Legacy discovery/import residue",
    },
}
_RESIDENTIAL_SCHEDULES = (
    "nc-progress-leaf-500",
    "nc-progress-leaf-501",
    "nc-progress-leaf-502",
    "nc-progress-leaf-503",
    "nc-progress-leaf-504",
)


def _connect(database_path: Path | None = None) -> sqlite3.Connection:
    path = Path(database_path or "data/db/duke_rates.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def build_dep_storm_rider_audit(database_path: Path | None = None) -> dict[str, Any]:
    conn = _connect(database_path)
    try:
        rows = _load_rows(conn)
    finally:
        conn.close()

    return {
        "generated_at": date.today().isoformat(),
        "family_count": len(rows),
        "status_counts": dict(sorted(Counter(str(row["audit_status"]) for row in rows).items())),
        "recommended_action_counts": dict(
            sorted(Counter(str(row["recommended_action"]) for row in rows).items())
        ),
        "rows": rows,
    }


def export_dep_storm_rider_audit(
    output_dir: Path,
    *,
    database_path: Path | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_dep_storm_rider_audit(database_path)

    rows_csv = output_dir / "dep_storm_rider_audit_rows.csv"
    summary_json = output_dir / "dep_storm_rider_audit_summary.json"
    markdown_path = output_dir / "dep_storm_rider_audit.md"

    _write_csv(rows_csv, report["rows"])
    summary_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")

    return {
        "rows_csv": rows_csv,
        "summary_json": summary_json,
        "markdown": markdown_path,
    }


def _load_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ", ".join("?" for _ in _FAMILY_HINTS)
    schedule_placeholders = ", ".join("?" for _ in _RESIDENTIAL_SCHEDULES)
    query = f"""
        WITH latest_runs AS (
            SELECT hpr.*
            FROM historical_processing_runs hpr
            INNER JOIN (
                SELECT historical_document_id, MAX(id) AS max_id
                FROM historical_processing_runs
                GROUP BY historical_document_id
            ) latest
              ON latest.max_id = hpr.id
        ),
        family_stats AS (
            SELECT
                tf.family_key,
                tf.title,
                COUNT(DISTINCT tv.id) AS version_count,
                COUNT(DISTINCT CASE WHEN COALESCE(vcs.charge_count, 0) > 0 THEN tv.id END) AS versions_with_charges,
                COUNT(DISTINCT CASE WHEN COALESCE(vcs.charge_count, 0) = 0 AND tv.id IS NOT NULL THEN tv.id END) AS zero_charge_version_count,
                COUNT(DISTINCT tv.historical_document_id) AS historical_doc_count,
                COUNT(DISTINCT CASE WHEN hd.start_page IS NOT NULL AND hd.end_page IS NOT NULL THEN hd.id END) AS bounded_doc_count,
                COUNT(DISTINCT CASE WHEN hd.start_page IS NULL OR hd.end_page IS NULL THEN hd.id END) AS unbounded_doc_count,
                MIN(tv.effective_start) AS earliest_effective_start,
                MAX(tv.effective_start) AS latest_effective_start,
                SUM(CASE WHEN lr.outcome_quality = 'strong' THEN 1 ELSE 0 END) AS strong_run_count,
                SUM(CASE WHEN lr.outcome_quality = 'weak' THEN 1 ELSE 0 END) AS weak_run_count,
                SUM(CASE WHEN lr.outcome_quality = 'empty' THEN 1 ELSE 0 END) AS empty_run_count,
                SUM(CASE WHEN lr.outcome_quality = 'skipped' THEN 1 ELSE 0 END) AS skipped_run_count
            FROM tariff_families tf
            LEFT JOIN tariff_versions tv
              ON tv.family_key = tf.family_key
            LEFT JOIN v_version_charge_summary vcs
              ON vcs.version_id = tv.id
            LEFT JOIN historical_documents hd
              ON hd.id = tv.historical_document_id
            LEFT JOIN latest_runs lr
              ON lr.historical_document_id = tv.historical_document_id
            WHERE tf.family_key IN ({placeholders})
            GROUP BY tf.family_key, tf.title
        ),
        discovery AS (
            SELECT
                json_each.value AS family_key,
                COUNT(*) AS discovery_record_count,
                COUNT(DISTINCT CASE
                    WHEN COALESCE(TRIM(dr.content_hash), '') <> '' THEN dr.content_hash
                    WHEN COALESCE(TRIM(dr.local_path), '') <> '' THEN dr.local_path
                    WHEN COALESCE(TRIM(dr.viewer_url), '') <> '' THEN dr.viewer_url
                    ELSE NULL
                END) AS unique_artifact_count
            FROM ncuc_discovery_records dr
            JOIN json_each(dr.family_keys_json)
            WHERE json_each.value IN ({placeholders})
            GROUP BY json_each.value
        ),
        applicability AS (
            SELECT
                rider_family_key AS family_key,
                COUNT(*) AS applicability_link_count,
                COUNT(DISTINCT applies_to_family_key) AS applies_to_family_count,
                SUM(CASE WHEN applies_to_family_key IN ({schedule_placeholders}) THEN 1 ELSE 0 END) AS residential_schedule_link_count
            FROM rider_applicability
            WHERE rider_family_key IN ({placeholders})
            GROUP BY rider_family_key
        )
        SELECT
            fs.*,
            COALESCE(discovery.discovery_record_count, 0) AS discovery_record_count,
            COALESCE(discovery.unique_artifact_count, 0) AS unique_artifact_count,
            COALESCE(applicability.applicability_link_count, 0) AS applicability_link_count,
            COALESCE(applicability.applies_to_family_count, 0) AS applies_to_family_count,
            COALESCE(applicability.residential_schedule_link_count, 0) AS residential_schedule_link_count
        FROM family_stats fs
        LEFT JOIN discovery
          ON discovery.family_key = fs.family_key
        LEFT JOIN applicability
          ON applicability.family_key = fs.family_key
        ORDER BY fs.family_key
    """
    params = tuple(_FAMILY_HINTS) + tuple(_FAMILY_HINTS) + tuple(_RESIDENTIAL_SCHEDULES) + tuple(_FAMILY_HINTS)
    raw_rows = conn.execute(query, params).fetchall()

    charged_rows = {
        str(row["family_key"]): int(row["versions_with_charges"] or 0)
        for row in raw_rows
    }
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        family_key = str(raw["family_key"])
        hint = _FAMILY_HINTS[family_key]
        audit_status, action, reason = _classify_row(raw, charged_rows)
        rows.append(
            {
                "family_key": family_key,
                "rider_label": hint["rider_label"],
                "title": raw["title"],
                "docket_hint": hint["docket_hint"],
                "search_hint": hint["search_hint"],
                "discovery_record_count": int(raw["discovery_record_count"] or 0),
                "unique_artifact_count": int(raw["unique_artifact_count"] or 0),
                "version_count": int(raw["version_count"] or 0),
                "versions_with_charges": int(raw["versions_with_charges"] or 0),
                "zero_charge_version_count": int(raw["zero_charge_version_count"] or 0),
                "historical_doc_count": int(raw["historical_doc_count"] or 0),
                "bounded_doc_count": int(raw["bounded_doc_count"] or 0),
                "unbounded_doc_count": int(raw["unbounded_doc_count"] or 0),
                "strong_run_count": int(raw["strong_run_count"] or 0),
                "weak_run_count": int(raw["weak_run_count"] or 0),
                "empty_run_count": int(raw["empty_run_count"] or 0),
                "skipped_run_count": int(raw["skipped_run_count"] or 0),
                "earliest_effective_start": raw["earliest_effective_start"],
                "latest_effective_start": raw["latest_effective_start"],
                "applicability_link_count": int(raw["applicability_link_count"] or 0),
                "applies_to_family_count": int(raw["applies_to_family_count"] or 0),
                "residential_schedule_link_count": int(raw["residential_schedule_link_count"] or 0),
                "audit_status": audit_status,
                "recommended_action": action,
                "reason": reason,
            }
        )
    return rows


def _classify_row(
    row: sqlite3.Row,
    charged_rows: dict[str, int],
) -> tuple[str, str, str]:
    family_key = str(row["family_key"])
    versions_with_charges = int(row["versions_with_charges"] or 0)
    zero_charge_version_count = int(row["zero_charge_version_count"] or 0)
    residential_schedule_link_count = int(row["residential_schedule_link_count"] or 0)
    historical_doc_count = int(row["historical_doc_count"] or 0)
    bounded_doc_count = int(row["bounded_doc_count"] or 0)
    discovery_record_count = int(row["discovery_record_count"] or 0)
    weak_run_count = int(row["weak_run_count"] or 0)
    empty_run_count = int(row["empty_run_count"] or 0)
    skipped_run_count = int(row["skipped_run_count"] or 0)

    if family_key.startswith("nc-progress-doc-"):
        if charged_rows.get("nc-progress-leaf-607", 0) > 0 or charged_rows.get("nc-progress-leaf-613", 0) > 0:
            return (
                "legacy_duplicate_family",
                "retire_or_reclassify_legacy_family",
                "Legacy doc-* storm family overlaps with charged canonical leaf families and should not remain authoritative.",
            )
        return (
            "legacy_family_without_canonical_replacement",
            "review_family_classification",
            "Legacy doc-* storm family exists without a clear charged canonical replacement.",
        )
    if versions_with_charges == 0 and discovery_record_count == 0:
        return (
            "missing_from_discovery",
            "authenticated_dragnet_search",
            "No discovery or charged versions yet for this storm rider family.",
        )
    if versions_with_charges == 0:
        return (
            "versions_present_but_no_charges",
            "inspect_source_pdf",
            "Storm rider family has versions, but none currently extract charges.",
        )
    if residential_schedule_link_count == 0:
        return (
            "charged_but_unlinked_to_residential_schedules",
            "add_applicability_links_after_canonical_review",
            "Storm rider family has charged versions but is not linked into the residential schedule applicability model.",
        )
    if zero_charge_version_count > 0 or weak_run_count > 0 or empty_run_count > 0 or skipped_run_count > 0:
        return (
            "charged_with_residual_parse_debt",
            "reparse_or_retire_residue",
            "Storm rider family has charged versions, but zero-charge or weak historical residue remains.",
        )
    if historical_doc_count > 0 and bounded_doc_count == 0:
        return (
            "charged_but_unbounded_history",
            "mine_bundle_page_spans",
            "Storm rider family is charged, but historical documents are still whole-PDF rather than bounded leaves.",
        )
    return (
        "healthy_canonical_candidate",
        "none",
        "Storm rider family has charged versions and no immediate parser/dependency blockers.",
    )


def _write_csv(path: Path, rows: object) -> None:
    items = list(rows)  # type: ignore[arg-type]
    if not items:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(items[0].keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(items)


def _render_markdown(report: dict[str, Any]) -> str:
    rows = list(report["rows"])
    lines = [
        "# DEP Storm Rider Audit",
        "",
        f"Generated from SQLite on {report['generated_at']}.",
        "",
        "This audit compares DEP storm-related rider families to help decide which families are canonical,",
        "which are legacy duplicates, and which still need applicability or parser cleanup.",
        "",
        f"- Families audited: {report['family_count']}",
        "",
        "Status counts:",
    ]
    for status, count in dict(report["status_counts"]).items():
        lines.append(f"- `{status}`: {count}")
    lines.extend(
        [
            "",
            "Recommended action counts:",
        ]
    )
    for action, count in dict(report["recommended_action_counts"]).items():
        lines.append(f"- `{action}`: {count}")
    lines.extend(
        [
            "",
            "## Ranked Family Status",
            "",
            _render_table(rows),
            "",
            "## Notes",
            "",
            "- `healthy_canonical_candidate`: charged, materially usable storm family with no immediate blockers.",
            "- `charged_but_unlinked_to_residential_schedules`: likely usable, but not yet linked into the applicability model.",
            "- `charged_with_residual_parse_debt`: usable family that still has zero-charge or weak historical residue.",
            "- `legacy_duplicate_family`: doc-* or duplicate family that overlaps a better canonical leaf family.",
        ]
    )
    return "\n".join(lines)


def _render_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_No storm rider families detected._"
    header = "Rider                  Status                                 Versions  Charged  Zero  Docs  Bound  ResLinks  First       Latest"
    body = []
    for row in rows:
        body.append(
            f"{str(row['rider_label']):<21}  "
            f"{str(row['audit_status']):<37}  "
            f"{int(row['version_count']):>8}  "
            f"{int(row['versions_with_charges']):>7}  "
            f"{int(row['zero_charge_version_count']):>4}  "
            f"{int(row['historical_doc_count']):>4}  "
            f"{int(row['bounded_doc_count']):>5}  "
            f"{int(row['residential_schedule_link_count']):>8}  "
            f"{str(row['earliest_effective_start'] or '-'): <10}  "
            f"{str(row['latest_effective_start'] or '-'): <10}"
        )
    return "```text\n" + "\n".join([header, *body]) + "\n```"


__all__ = [
    "build_dep_storm_rider_audit",
    "export_dep_storm_rider_audit",
    "_DEFAULT_OUTPUT_DIR",
]
