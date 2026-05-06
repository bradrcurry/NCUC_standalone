from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

_DEFAULT_OUTPUT_DIR = Path("docs/reports/dep_compliance_bundle_audit")
_FAMILY_HINTS: dict[str, dict[str, str]] = {
    "nc-progress-leaf-604": {
        "rider_label": "EDIT-4",
        "search_hint": 'E-2 Sub 1196 | "EDIT-4" | "Leaf No. 604"',
        "docket_hint": "E-2 Sub 1196",
    },
    "nc-progress-leaf-605": {
        "rider_label": "CPRE",
        "search_hint": 'E-2 Sub 1109 | "CPRE" | "Leaf No. 605"',
        "docket_hint": "E-2 Sub 1109",
    },
    "nc-progress-leaf-608": {
        "rider_label": "RDM",
        "search_hint": 'E-2 Sub 1294 | "Rider RDM" | "Leaf No. 608"',
        "docket_hint": "E-2 Sub 1294",
    },
    "nc-progress-leaf-609": {
        "rider_label": "ESM",
        "search_hint": 'E-2 annual compliance | "Rider ESM" | "Leaf No. 609"',
        "docket_hint": "Annual compliance / PBR bundle",
    },
    "nc-progress-leaf-610": {
        "rider_label": "PIM",
        "search_hint": 'E-2 Sub 1108 | "Rider PIM" | "Leaf No. 610"',
        "docket_hint": "E-2 Sub 1108",
    },
    "nc-progress-leaf-611": {
        "rider_label": "CAR",
        "search_hint": 'E-2 Sub 1252 | "Rider CAR" | "Leaf No. 611"',
        "docket_hint": "E-2 Sub 1252",
    },
}


def _connect(database_path: Path | None = None) -> sqlite3.Connection:
    path = Path(database_path or "data/db/duke_rates.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def build_dep_compliance_bundle_audit(
    database_path: Path | None = None,
) -> dict[str, Any]:
    conn = _connect(database_path)
    try:
        rows = _load_rows(conn)
    finally:
        conn.close()

    status_counts = Counter(str(row["audit_status"]) for row in rows)
    recommendation_counts = Counter(str(row["recommended_action"]) for row in rows)
    return {
        "generated_at": date.today().isoformat(),
        "family_count": len(rows),
        "status_counts": dict(sorted(status_counts.items())),
        "recommended_action_counts": dict(sorted(recommendation_counts.items())),
        "rows": rows,
    }


def export_dep_compliance_bundle_audit(
    output_dir: Path,
    *,
    database_path: Path | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_dep_compliance_bundle_audit(database_path)

    rows_csv = output_dir / "dep_compliance_bundle_audit_rows.csv"
    summary_json = output_dir / "dep_compliance_bundle_audit_summary.json"
    markdown_path = output_dir / "dep_compliance_bundle_audit.md"

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
    query = f"""
        WITH discovery AS (
            SELECT
                json_each.value AS family_key,
                COUNT(*) AS discovery_record_count,
                SUM(CASE WHEN dr.fetch_status = 'success' THEN 1 ELSE 0 END) AS success_record_count,
                SUM(CASE WHEN COALESCE(dr.local_path, '') <> '' THEN 1 ELSE 0 END) AS downloaded_record_count,
                COUNT(DISTINCT CASE
                    WHEN COALESCE(TRIM(dr.content_hash), '') <> '' THEN dr.content_hash
                    WHEN COALESCE(TRIM(dr.local_path), '') <> '' THEN dr.local_path
                    WHEN COALESCE(TRIM(dr.attachment_url), '') <> '' THEN dr.attachment_url
                    WHEN COALESCE(TRIM(dr.viewer_url), '') <> '' THEN dr.viewer_url
                    WHEN COALESCE(TRIM(dr.discovered_url), '') <> '' THEN dr.discovered_url
                    ELSE NULL
                END) AS unique_hash_count
            FROM ncuc_discovery_records dr
            JOIN json_each(dr.family_keys_json)
            WHERE json_each.value IN ({placeholders})
            GROUP BY json_each.value
        ),
        historical AS (
            SELECT
                hd.family_key,
                COUNT(*) AS historical_doc_count,
                SUM(CASE WHEN hd.start_page IS NOT NULL AND hd.end_page IS NOT NULL THEN 1 ELSE 0 END) AS bounded_doc_count,
                SUM(CASE WHEN hd.start_page IS NULL OR hd.end_page IS NULL THEN 1 ELSE 0 END) AS unbounded_doc_count
            FROM historical_documents hd
            WHERE hd.family_key IN ({placeholders})
            GROUP BY hd.family_key
        ),
        latest_runs AS (
            SELECT hpr.*
            FROM historical_processing_runs hpr
            INNER JOIN (
                SELECT historical_document_id, MAX(id) AS max_id
                FROM historical_processing_runs
                GROUP BY historical_document_id
            ) latest
              ON latest.max_id = hpr.id
        ),
        versions AS (
            SELECT
                tv.family_key,
                COUNT(DISTINCT tv.id) AS version_count,
                COUNT(DISTINCT CASE WHEN tv.source_type <> 'utility_current' THEN tv.id END) AS regulator_version_count,
                COUNT(DISTINCT CASE WHEN vcs.charge_count > 0 THEN tv.id END) AS versions_with_charges,
                COUNT(DISTINCT CASE WHEN vcs.charge_count = 0 AND tv.id IS NOT NULL THEN tv.id END) AS zero_charge_version_count,
                SUM(CASE WHEN lr.outcome_quality = 'strong' THEN 1 ELSE 0 END) AS strong_run_count,
                SUM(CASE WHEN lr.outcome_quality = 'weak' THEN 1 ELSE 0 END) AS weak_run_count,
                SUM(CASE WHEN lr.outcome_quality = 'empty' THEN 1 ELSE 0 END) AS empty_run_count,
                SUM(CASE WHEN lr.outcome_quality = 'skipped' THEN 1 ELSE 0 END) AS skipped_run_count
            FROM tariff_versions tv
            LEFT JOIN v_version_charge_summary vcs
              ON vcs.version_id = tv.id
            LEFT JOIN latest_runs lr
              ON lr.historical_document_id = tv.historical_document_id
            WHERE tv.family_key IN ({placeholders})
            GROUP BY tv.family_key
        )
        SELECT
            tf.family_key,
            tf.title,
            COALESCE(discovery.discovery_record_count, 0) AS discovery_record_count,
            COALESCE(discovery.success_record_count, 0) AS success_record_count,
            COALESCE(discovery.downloaded_record_count, 0) AS downloaded_record_count,
            COALESCE(discovery.unique_hash_count, 0) AS unique_hash_count,
            COALESCE(historical.historical_doc_count, 0) AS historical_doc_count,
            COALESCE(historical.bounded_doc_count, 0) AS bounded_doc_count,
            COALESCE(historical.unbounded_doc_count, 0) AS unbounded_doc_count,
            COALESCE(versions.version_count, 0) AS version_count,
            COALESCE(versions.regulator_version_count, 0) AS regulator_version_count,
            COALESCE(versions.versions_with_charges, 0) AS versions_with_charges,
            COALESCE(versions.zero_charge_version_count, 0) AS zero_charge_version_count,
            COALESCE(versions.strong_run_count, 0) AS strong_run_count,
            COALESCE(versions.weak_run_count, 0) AS weak_run_count,
            COALESCE(versions.empty_run_count, 0) AS empty_run_count,
            COALESCE(versions.skipped_run_count, 0) AS skipped_run_count
        FROM tariff_families tf
        LEFT JOIN discovery
          ON discovery.family_key = tf.family_key
        LEFT JOIN historical
          ON historical.family_key = tf.family_key
        LEFT JOIN versions
          ON versions.family_key = tf.family_key
        WHERE tf.family_key IN ({placeholders})
        ORDER BY tf.family_key
    """
    params = tuple(_FAMILY_HINTS) * 4
    raw_rows = conn.execute(query, params).fetchall()

    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        family_key = str(raw["family_key"])
        hint = _FAMILY_HINTS[family_key]
        audit_status, recommended_action, reason = _classify_row(raw)
        rows.append(
            {
                "family_key": family_key,
                "rider_label": hint["rider_label"],
                "title": raw["title"],
                "docket_hint": hint["docket_hint"],
                "search_hint": hint["search_hint"],
                "discovery_record_count": int(raw["discovery_record_count"] or 0),
                "success_record_count": int(raw["success_record_count"] or 0),
                "downloaded_record_count": int(raw["downloaded_record_count"] or 0),
                "unique_artifact_count": int(raw["unique_hash_count"] or 0),
                "duplicate_download_surplus": max(
                    int(raw["downloaded_record_count"] or 0) - int(raw["unique_hash_count"] or 0),
                    0,
                ),
                "historical_doc_count": int(raw["historical_doc_count"] or 0),
                "bounded_doc_count": int(raw["bounded_doc_count"] or 0),
                "unbounded_doc_count": int(raw["unbounded_doc_count"] or 0),
                "version_count": int(raw["version_count"] or 0),
                "regulator_version_count": int(raw["regulator_version_count"] or 0),
                "versions_with_charges": int(raw["versions_with_charges"] or 0),
                "zero_charge_version_count": int(raw["zero_charge_version_count"] or 0),
                "strong_run_count": int(raw["strong_run_count"] or 0),
                "weak_run_count": int(raw["weak_run_count"] or 0),
                "empty_run_count": int(raw["empty_run_count"] or 0),
                "skipped_run_count": int(raw["skipped_run_count"] or 0),
                "audit_status": audit_status,
                "recommended_action": recommended_action,
                "reason": reason,
            }
        )
    return rows


def _classify_row(row: sqlite3.Row) -> tuple[str, str, str]:
    discovery_record_count = int(row["discovery_record_count"] or 0)
    downloaded_record_count = int(row["downloaded_record_count"] or 0)
    unique_artifact_count = int(row["unique_hash_count"] or 0)
    historical_doc_count = int(row["historical_doc_count"] or 0)
    bounded_doc_count = int(row["bounded_doc_count"] or 0)
    versions_with_charges = int(row["versions_with_charges"] or 0)
    zero_charge_version_count = int(row["zero_charge_version_count"] or 0)
    weak_run_count = int(row["weak_run_count"] or 0)
    empty_run_count = int(row["empty_run_count"] or 0)
    skipped_run_count = int(row["skipped_run_count"] or 0)

    if discovery_record_count == 0:
        return (
            "missing_from_discovery",
            "authenticated_dragnet_search",
            "No NCUC discovery records are linked to this rider family yet.",
        )
    if downloaded_record_count > 0 and unique_artifact_count < downloaded_record_count and historical_doc_count == 0:
        return (
            "downloaded_duplicate_candidates",
            "deduplicate_then_import_bundle",
            "Downloaded portal records appear to collapse to fewer unique file hashes before import.",
        )
    if downloaded_record_count > 0 and historical_doc_count == 0:
        return (
            "downloaded_not_imported",
            "import_discovered_bundle",
            "Downloaded NCUC records exist, but no historical_documents rows are linked yet.",
        )
    if historical_doc_count > 0 and bounded_doc_count == 0:
        return (
            "imported_but_unbounded",
            "mine_bundle_page_spans",
            "Historical documents exist, but none are bounded to rider page spans yet.",
        )
    if bounded_doc_count > 0 and versions_with_charges == 0:
        return (
            "bounded_but_zero_charge",
            "reparse_existing_bundle_spans",
            "Bounded historical spans exist, but they still produce zero extracted charges.",
        )
    if versions_with_charges > 0 and (
        zero_charge_version_count > 0 or weak_run_count > 0 or empty_run_count > 0 or skipped_run_count > 0
    ):
        return (
            "bounded_but_partial",
            "audit_bundle_quality_and_reparse",
            "Family has some successful extracted versions, but weak/empty/zero-charge versions remain.",
        )
    return (
        "healthy",
        "none",
        "Discovery, import, bounds, and extracted charge coverage all look populated for this family.",
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
        "# DEP Compliance Bundle Audit",
        "",
        f"Generated from SQLite on {report['generated_at']}.",
        "",
        "This audit is focused on the DEP residential rider families currently driving backfill work:",
        "`leaf-604`, `leaf-605`, `leaf-608`, `leaf-609`, `leaf-610`, and `leaf-611`.",
        "",
        f"- Families audited: {report['family_count']}",
        "",
        "Status counts:",
    ]
    for status, count in dict(report["status_counts"]).items():
        lines.append(f"- `{status}`: {count}")
    lines.extend(["", "Recommended action counts:"])
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
            "- `missing_from_discovery`: no portal discovery rows yet; start with authenticated docket/bundle search.",
            "- `downloaded_not_imported`: the portal fetch exists locally, but it has not been promoted into `historical_documents`.",
            "- `imported_but_unbounded`: bundle PDFs were imported, but rider page spans are still whole-PDF or null.",
            "- `bounded_but_zero_charge`: page spans exist, but parsing/extraction is still not yielding charge rows.",
            "- `bounded_but_partial`: some versions are healthy, but the family still has weak, empty, skipped, or zero-charge residue.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_No bundle audit rows generated._"
    header = "Rider   Status                        Discovery  Docs  Bound  Charged  Empty  Weak  Zero  Action"
    body = []
    for row in rows:
        body.append(
            f"{str(row['rider_label']):<6}  "
            f"{str(row['audit_status']):<28}  "
            f"{int(row['downloaded_record_count']):>9}  "
            f"{int(row['historical_doc_count']):>4}  "
            f"{int(row['bounded_doc_count']):>5}  "
            f"{int(row['versions_with_charges']):>7}  "
            f"{int(row['empty_run_count']):>5}  "
            f"{int(row['weak_run_count']):>4}  "
            f"{int(row['zero_charge_version_count']):>4}  "
            f"{str(row['recommended_action'])}"
        )
    return "```text\n" + "\n".join([header, *body]) + "\n```"


__all__ = [
    "build_dep_compliance_bundle_audit",
    "export_dep_compliance_bundle_audit",
    "_DEFAULT_OUTPUT_DIR",
]
