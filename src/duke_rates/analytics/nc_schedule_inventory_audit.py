from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path

from duke_rates.analytics.nc_coverage_assessment import get_nc_coverage_families

_DEFAULT_OUTPUT_DIR = Path("docs/reports/nc_schedule_inventory_audit")
_LEAF_RE = re.compile(r"-leaf-(\d+)$")


def _connect(database_path: Path | None = None) -> sqlite3.Connection:
    path = Path(database_path or "data/db/duke_rates.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def build_nc_schedule_inventory_audit(database_path: Path | None = None) -> dict[str, object]:
    matrix_scope = {
        family.family_key
        for families in get_nc_coverage_families().values()
        for family in families
    }
    conn = _connect(database_path)
    try:
        rows = _load_rows(conn, matrix_scope)
    finally:
        conn.close()

    status_counts = Counter(str(row["tracking_status"]) for row in rows)
    scope_counts = Counter(str(row["matrix_scope_status"]) for row in rows)
    billing_counts = Counter(str(row["billing_class"]) for row in rows)

    return {
        "generated_at": date.today().isoformat(),
        "total_families": len(rows),
        "matrix_scope_size": len(matrix_scope),
        "status_counts": dict(sorted(status_counts.items())),
        "matrix_scope_counts": dict(sorted(scope_counts.items())),
        "billing_class_counts": dict(sorted(billing_counts.items())),
        "rows": rows,
    }


def export_nc_schedule_inventory_audit(
    output_dir: Path,
    *,
    database_path: Path | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_nc_schedule_inventory_audit(database_path)

    rows_csv = output_dir / "nc_schedule_inventory_rows.csv"
    summary_json = output_dir / "nc_schedule_inventory_summary.json"
    markdown_path = output_dir / "nc_schedule_inventory_audit.md"

    _write_csv(rows_csv, report["rows"])
    summary_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")

    return {
        "rows_csv": rows_csv,
        "summary_json": summary_json,
        "markdown": markdown_path,
    }


def _load_rows(conn: sqlite3.Connection, matrix_scope: set[str]) -> list[dict[str, object]]:
    raw_rows = conn.execute(
        """
        WITH family_summary AS (
            SELECT
                tf.family_key,
                tf.state,
                tf.company,
                tf.family_type,
                tf.title,
                tf.tariff_identifier,
                tf.schedule_code,
                tf.current_document_id,
                tf.notes,
                COUNT(DISTINCT tv.id) AS version_count,
                COUNT(DISTINCT CASE WHEN vcs.charge_count > 0 THEN tv.id END) AS versions_with_charges,
                COUNT(DISTINCT CASE WHEN vcs.charge_count = 0 AND tv.id IS NOT NULL THEN tv.id END) AS versions_zero_charges,
                COALESCE(SUM(vcs.charge_count), 0) AS total_charge_rows,
                COUNT(DISTINCT tv.historical_document_id) AS linked_historical_docs,
                MIN(tv.effective_start) AS earliest_effective_start,
                MAX(tv.effective_start) AS latest_effective_start
            FROM tariff_families tf
            LEFT JOIN tariff_versions tv
              ON tv.family_key = tf.family_key
            LEFT JOIN v_version_charge_summary vcs
              ON vcs.version_id = tv.id
            WHERE tf.state = 'NC'
              AND LOWER(tf.company) IN ('progress', 'carolinas')
              AND tf.family_type = 'rate_schedule'
            GROUP BY
                tf.family_key,
                tf.state,
                tf.company,
                tf.family_type,
                tf.title,
                tf.tariff_identifier,
                tf.schedule_code,
                tf.current_document_id,
                tf.notes
        )
        SELECT *
        FROM family_summary
        ORDER BY company, family_key
        """
    ).fetchall()

    rows: list[dict[str, object]] = []
    for row in raw_rows:
        family_key = str(row["family_key"])
        billing_class = _billing_class(family_key, str(row["title"] or ""))
        matrix_scope_status = "included_in_matrix" if family_key in matrix_scope else "missing_from_matrix"
        tracking_status, recommended_action, reason = _tracking_status(
            row,
            matrix_scope_status=matrix_scope_status,
            billing_class=billing_class,
        )
        rows.append(
            {
                "utility": "DEP" if str(row["company"]).lower() == "progress" else "DEC",
                "company": row["company"],
                "family_key": family_key,
                "title": row["title"],
                "tariff_identifier": row["tariff_identifier"],
                "schedule_code": row["schedule_code"],
                "leaf_no": _extract_leaf_no(family_key),
                "version_count": int(row["version_count"] or 0),
                "versions_with_charges": int(row["versions_with_charges"] or 0),
                "versions_zero_charges": int(row["versions_zero_charges"] or 0),
                "total_charge_rows": int(row["total_charge_rows"] or 0),
                "linked_historical_docs": int(row["linked_historical_docs"] or 0),
                "has_current_document": row["current_document_id"] is not None,
                "earliest_effective_start": row["earliest_effective_start"],
                "latest_effective_start": row["latest_effective_start"],
                "matrix_scope_status": matrix_scope_status,
                "billing_class": billing_class,
                "tracking_status": tracking_status,
                "recommended_action": recommended_action,
                "reason": reason,
                "family_notes": row["notes"],
            }
        )
    return rows


def _extract_leaf_no(family_key: str) -> str | None:
    match = _LEAF_RE.search(family_key)
    return match.group(1) if match else None


def _billing_class(family_key: str, title: str) -> str:
    title_upper = title.upper()
    if "-doc-" in family_key:
        return "legacy_or_malformed_family"
    if family_key.startswith("nc-progress-leaf-"):
        leaf_no = _extract_leaf_no(family_key)
        if leaf_no is None:
            return "other_schedule"
        leaf_num = int(leaf_no)
        if 740 <= leaf_num <= 799 or "EV " in title_upper or title_upper.startswith("ELECTRIC VEHICLE") or "EV OVERNIGHT" in title_upper:
            return "ev_or_pilot_schedule"
        if 500 <= leaf_num <= 599:
            return "core_billing_schedule"
        if 700 <= leaf_num <= 739:
            return "program_or_credit_schedule"
        if 800 <= leaf_num <= 899:
            return "regulation_or_terms_schedule"
        return "other_schedule"
    if family_key.startswith("nc-carolinas-schedule-"):
        code = family_key.split("schedule-", 1)[1]
        if code in {"RS", "RT", "SGS", "LGS", "ES", "I", "PG", "TS", "HP", "HLF", "PP", "PPBE", "RE", "BC"}:
            return "core_billing_schedule"
        if code in {"NL", "OL", "PL"}:
            return "lighting_schedule"
        return "other_schedule"
    return "other_schedule"


def _tracking_status(
    row: sqlite3.Row,
    *,
    matrix_scope_status: str,
    billing_class: str,
) -> tuple[str, str, str]:
    version_count = int(row["version_count"] or 0)
    versions_with_charges = int(row["versions_with_charges"] or 0)
    total_charge_rows = int(row["total_charge_rows"] or 0)
    family_key = str(row["family_key"])

    if billing_class == "legacy_or_malformed_family":
        return (
            "legacy_duplicate_or_needs_reclassification",
            "review_family_classification",
            "Family key uses the legacy doc-* pattern and should not be treated as canonical current inventory.",
        )
    if version_count == 0:
        return (
            "no_versions",
            "search_or_seed_family_versions",
            "Family exists in tariff_families but has no tariff_versions.",
        )
    if versions_with_charges == 0:
        return (
            "versions_present_but_no_charges",
            "inspect_source_pdf",
            "Family has versions but no extracted charges yet.",
        )
    if matrix_scope_status == "missing_from_matrix" and billing_class == "core_billing_schedule":
        return (
            "missing_from_matrix_but_db_populated",
            "expand_coverage_scope",
            f"Billing-relevant family is populated in SQLite but omitted from the current coverage matrix ({family_key}).",
        )
    if matrix_scope_status == "missing_from_matrix" and billing_class in {"ev_or_pilot_schedule", "lighting_schedule"}:
        return (
            "out_of_scope_but_db_populated",
            "decide_if_matrix_should_expand",
            "Family is populated in SQLite but currently outside the focused matrix scope.",
        )
    if matrix_scope_status == "missing_from_matrix":
        return (
            "out_of_scope_reference_family",
            "leave_out_of_matrix_or_add_secondary_inventory",
            "Family is populated but appears to be a program/terms/reference schedule rather than a core matrix row.",
        )
    if total_charge_rows > 0:
        return (
            "covered_and_populated",
            "none",
            "Family is included in the focused matrix scope and has extracted charges.",
        )
    return (
        "covered_but_needs_work",
        "inspect_source_pdf",
        "Family is in matrix scope but still lacks usable extracted charges.",
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


def _render_markdown(report: dict[str, object]) -> str:
    rows = list(report["rows"])  # type: ignore[arg-type]
    key_rows = [
        row for row in rows
        if row["tracking_status"] in {
            "missing_from_matrix_but_db_populated",
            "legacy_duplicate_or_needs_reclassification",
            "versions_present_but_no_charges",
        }
    ]
    top_rows = key_rows[:40]
    lines = [
        "# NC Schedule Inventory Audit",
        "",
        f"Generated from SQLite on {report['generated_at']}.",
        "",
        f"- Total NC rate_schedule families: {report['total_families']}",
        f"- Current matrix scope size: {report['matrix_scope_size']}",
        "",
        "Tracking status counts:",
    ]
    for status, count in dict(report["status_counts"]).items():  # type: ignore[arg-type]
        lines.append(f"- `{status}`: {count}")
    lines.extend(["", "Billing class counts:"])
    for status, count in dict(report["billing_class_counts"]).items():  # type: ignore[arg-type]
        lines.append(f"- `{status}`: {count}")
    lines.extend(["", "## Key families to review", "", _render_table(top_rows), ""])
    return "\n".join(lines)


def _render_table(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "_No inventory issues detected._"
    header = "Utility  Family Key                           Scope              Billing Class                  Versions  Charged  Status"
    body = []
    for row in rows:
        body.append(
            f"{str(row['utility']):<7}  "
            f"{str(row['family_key']):<35}  "
            f"{str(row['matrix_scope_status']):<17}  "
            f"{str(row['billing_class']):<28}  "
            f"{int(row['version_count']):>8}  "
            f"{int(row['versions_with_charges']):>7}  "
            f"{str(row['tracking_status'])}"
        )
    return "```text\n" + "\n".join([header, *body]) + "\n```"


__all__ = [
    "build_nc_schedule_inventory_audit",
    "export_nc_schedule_inventory_audit",
    "_DEFAULT_OUTPUT_DIR",
]
