from __future__ import annotations

import csv
import json
import sqlite3
from bisect import bisect_right
from datetime import date
from pathlib import Path
from typing import Any

from duke_rates.analytics.canonical_rider_components import load_dep_res_canonical_rider_components
from duke_rates.analytics.dep_validation import load_dep_res_validation_report

_DEFAULT_OUTPUT_DIR = Path("docs/reports/dep_leaf_503_audit")
_FAMILY_KEY = "nc-progress-leaf-503"
_SCHEDULE_LABEL = "R-TOU-CPP"


def _connect(database_path: Path | None = None) -> sqlite3.Connection:
    path = Path(database_path or "data/db/duke_rates.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def build_dep_leaf503_audit(database_path: Path | None = None) -> dict[str, Any]:
    validation_report = load_dep_res_validation_report(database_path=database_path)
    rider_components = load_dep_res_canonical_rider_components(database_path=database_path)

    conn = _connect(database_path)
    try:
        version_rows = _load_leaf503_versions(conn)
        rider_link_rows = _load_rider_links(conn)
    finally:
        conn.close()

    expected_riders = list(validation_report["summary"]["applicable_riders_by_schedule"][_SCHEDULE_LABEL])
    rider_series = _build_rider_series(rider_components)
    version_audit_rows = _build_version_audit_rows(version_rows, rider_series, expected_riders)

    summary = {
        "generated_at": date.today().isoformat(),
        "family_key": _FAMILY_KEY,
        "schedule_label": _SCHEDULE_LABEL,
        "version_count": len(version_rows),
        "rider_applicability_link_count": len(rider_link_rows),
        "expected_rider_code_count": len(expected_riders),
        "expected_rider_codes": expected_riders,
        "canonical_rider_effective_dates": [row["effective_date"] for row in rider_series],
        "canonical_rider_series_count": len(rider_series),
        "versions_with_rider_source_coverage": sum(1 for row in version_audit_rows if row["rider_coverage_status"] != "no_prior_rider_series"),
        "missing_rider_applicability_links": len(rider_link_rows) == 0,
    }

    return {
        "summary": summary,
        "versions": version_audit_rows,
        "rider_applicability_links": rider_link_rows,
    }


def export_dep_leaf503_audit(
    output_dir: Path,
    *,
    database_path: Path | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_dep_leaf503_audit(database_path)

    versions_csv = output_dir / "dep_leaf_503_versions.csv"
    links_csv = output_dir / "dep_leaf_503_rider_links.csv"
    summary_json = output_dir / "dep_leaf_503_audit_summary.json"
    markdown_path = output_dir / "dep_leaf_503_audit.md"

    _write_csv(versions_csv, report["versions"])
    _write_csv(links_csv, report["rider_applicability_links"])
    summary_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")

    return {
        "versions_csv": versions_csv,
        "links_csv": links_csv,
        "summary_json": summary_json,
        "markdown": markdown_path,
    }


def _load_leaf503_versions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            tv.id AS version_id,
            tv.effective_start,
            tv.effective_end,
            tv.revision_label,
            tv.source_type,
            tv.historical_document_id,
            hd.local_path AS historical_local_path,
            COUNT(tc.id) AS charge_count
        FROM tariff_versions tv
        LEFT JOIN tariff_charges tc
          ON tc.version_id = tv.id
        LEFT JOIN historical_documents hd
          ON hd.id = tv.historical_document_id
        WHERE tv.family_key = ?
        GROUP BY
            tv.id,
            tv.effective_start,
            tv.effective_end,
            tv.revision_label,
            tv.source_type,
            tv.historical_document_id,
            hd.local_path
        ORDER BY tv.effective_start, tv.id
        """,
        (_FAMILY_KEY,),
    ).fetchall()
    return [dict(row) for row in rows]


def _load_rider_links(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            rider_family_key,
            mandatory,
            in_rider_summary,
            enrollment_type,
            applicability_notes,
            effective_start,
            effective_end,
            source_type,
            confidence_score
        FROM rider_applicability
        WHERE applies_to_family_key = ?
        ORDER BY rider_family_key, effective_start
        """,
        (_FAMILY_KEY,),
    ).fetchall()
    return [dict(row) for row in rows]


def _build_rider_series(rider_components) -> list[dict[str, Any]]:
    if rider_components.empty:
        return []
    grouped = rider_components.groupby("effective_date")
    rows: list[dict[str, Any]] = []
    for effective_date, group in grouped:
        codes = sorted(set(group["rider_code"]))
        source_kinds = sorted(set(group["source_kind"]))
        rows.append(
            {
                "effective_date": effective_date.strftime("%Y-%m-%d"),
                "rider_codes": codes,
                "rider_code_count": len(codes),
                "source_kinds": source_kinds,
            }
        )
    rows.sort(key=lambda row: str(row["effective_date"]))
    return rows


def _build_version_audit_rows(
    version_rows: list[dict[str, Any]],
    rider_series: list[dict[str, Any]],
    expected_riders: list[str],
) -> list[dict[str, Any]]:
    rider_dates = [row["effective_date"] for row in rider_series]
    rows: list[dict[str, Any]] = []
    for version in version_rows:
        effective_start = str(version["effective_start"] or "")
        matched = None
        coverage_status = "no_prior_rider_series"
        if effective_start:
            idx = bisect_right(rider_dates, effective_start) - 1
            if idx >= 0:
                matched = rider_series[idx]
                coverage_status = "same_day" if matched["effective_date"] == effective_start else "carried_forward"
        matched_codes = matched["rider_codes"] if matched else []
        missing_codes = [code for code in expected_riders if code not in matched_codes]
        rows.append(
            {
                "version_id": int(version["version_id"]),
                "effective_start": version["effective_start"],
                "effective_end": version["effective_end"],
                "revision_label": version["revision_label"],
                "source_type": version["source_type"],
                "historical_document_id": version["historical_document_id"],
                "historical_local_path": version["historical_local_path"],
                "charge_count": int(version["charge_count"] or 0),
                "matched_rider_effective_date": matched["effective_date"] if matched else None,
                "rider_coverage_status": coverage_status,
                "matched_rider_code_count": len(matched_codes),
                "missing_expected_rider_count": len(missing_codes),
                "missing_expected_rider_codes": ",".join(missing_codes),
                "matched_source_kinds": ",".join(matched["source_kinds"]) if matched else "",
            }
        )
    return rows


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
    summary = report["summary"]
    lines = [
        "# DEP Leaf 503 Audit",
        "",
        f"Generated from SQLite on {summary['generated_at']}.",
        "",
        f"- Family: `{summary['family_key']}` (`{summary['schedule_label']}`)",
        f"- Base versions found: {summary['version_count']}",
        f"- Rider applicability links found: {summary['rider_applicability_link_count']}",
        f"- Expected rider codes from residential rider model: {summary['expected_rider_code_count']}",
        f"- Canonical rider effective dates available: {summary['canonical_rider_series_count']}",
        f"- Versions with rider-source coverage: {summary['versions_with_rider_source_coverage']}",
        "",
    ]
    if summary["missing_rider_applicability_links"]:
        lines.extend(
            [
                "Key finding:",
                "- `leaf-503` has base tariff versions and residential rider-source history, but no explicit `rider_applicability` links in SQLite.",
                "",
            ]
        )
    lines.extend(
        [
            "Expected rider codes:",
            "- " + ", ".join(summary["expected_rider_codes"]),
            "",
            "## Version Audit",
            "",
            _render_version_table(report["versions"]),
            "",
        ]
    )
    if report["rider_applicability_links"]:
        lines.extend(["## Rider Links", "", _render_link_table(report["rider_applicability_links"]), ""])
    else:
        lines.extend(["## Rider Links", "", "_No rider_applicability links found for `nc-progress-leaf-503`._", ""])
    return "\n".join(lines)


def _render_version_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_No versions found._"
    header = "Start       Source           Charges  Rider Date   Rider Cov.        Missing Riders"
    body = []
    for row in rows:
        body.append(
            f"{str(row['effective_start'] or '-'): <10}  "
            f"{str(row['source_type'] or '-'): <15}  "
            f"{int(row['charge_count']):>7}  "
            f"{str(row['matched_rider_effective_date'] or '-'): <10}  "
            f"{str(row['rider_coverage_status']):<16}  "
            f"{int(row['missing_expected_rider_count']):>14}"
        )
    return "```text\n" + "\n".join([header, *body]) + "\n```"


def _render_link_table(rows: list[dict[str, Any]]) -> str:
    header = "Rider Key                          Summary  Mandatory  Type"
    body = []
    for row in rows:
        body.append(
            f"{str(row['rider_family_key']):<34}  "
            f"{'Y' if row['in_rider_summary'] else 'N':<7}  "
            f"{'Y' if row['mandatory'] else 'N':<9}  "
            f"{str(row['enrollment_type'] or '-')}"
        )
    return "```text\n" + "\n".join([header, *body]) + "\n```"


__all__ = [
    "build_dep_leaf503_audit",
    "export_dep_leaf503_audit",
    "_DEFAULT_OUTPUT_DIR",
]
