from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

_DEFAULT_OUTPUT_DIR = Path("docs/reports/dep_residential_rider_gap_audit")
_BASE_SCHEDULES: tuple[tuple[str, str], ...] = (
    ("nc-progress-leaf-500", "RES"),
    ("nc-progress-leaf-501", "R-TOUD"),
    ("nc-progress-leaf-502", "R-TOU"),
    ("nc-progress-leaf-503", "R-TOU-CPP"),
    ("nc-progress-leaf-504", "R-TOU-EV"),
)


def _connect(database_path: Path | None = None) -> sqlite3.Connection:
    path = Path(database_path or "data/db/duke_rates.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def build_dep_residential_rider_gap_audit(database_path: Path | None = None) -> dict[str, Any]:
    conn = _connect(database_path)
    try:
        audit_rows = _build_rows(conn)
    finally:
        conn.close()

    summary = {
        "generated_at": date.today().isoformat(),
        "base_schedule_count": len(_BASE_SCHEDULES),
        "base_version_count": len({(row["base_family_key"], row["base_version_id"]) for row in audit_rows}),
        "linked_rider_family_count": len({row["rider_family_key"] for row in audit_rows}),
        "status_counts": dict(sorted(Counter(str(row["rider_status"]) for row in audit_rows).items())),
        "rows": audit_rows,
    }
    return summary


def export_dep_residential_rider_gap_audit(
    output_dir: Path,
    *,
    database_path: Path | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_dep_residential_rider_gap_audit(database_path)

    rows_csv = output_dir / "dep_residential_rider_gap_rows.csv"
    summary_json = output_dir / "dep_residential_rider_gap_summary.json"
    markdown_path = output_dir / "dep_residential_rider_gap_audit.md"

    _write_csv(rows_csv, report["rows"])
    summary_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")

    return {
        "rows_csv": rows_csv,
        "summary_json": summary_json,
        "markdown": markdown_path,
    }


def _build_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for base_family_key, schedule_label in _BASE_SCHEDULES:
        base_versions = conn.execute(
            """
            SELECT
                tv.id AS version_id,
                tv.effective_start,
                tv.effective_end,
                tv.revision_label,
                tv.source_type,
                COUNT(tc.id) AS charge_count
            FROM tariff_versions tv
            LEFT JOIN tariff_charges tc
              ON tc.version_id = tv.id
            WHERE tv.family_key = ?
            GROUP BY tv.id, tv.effective_start, tv.effective_end, tv.revision_label, tv.source_type
            ORDER BY tv.effective_start, tv.id
            """,
            (base_family_key,),
        ).fetchall()
        rider_links = conn.execute(
            """
            SELECT rider_family_key, mandatory, in_rider_summary, enrollment_type
            FROM rider_applicability
            WHERE applies_to_family_key = ?
            ORDER BY rider_family_key
            """,
            (base_family_key,),
        ).fetchall()

        for base_version in base_versions:
            for rider_link in rider_links:
                rider_status, selected_rider = _resolve_rider_status(
                    conn,
                    rider_family_key=str(rider_link["rider_family_key"]),
                    ref_date=str(base_version["effective_start"] or ""),
                )
                rows.append(
                    {
                        "schedule_label": schedule_label,
                        "base_family_key": base_family_key,
                        "base_version_id": int(base_version["version_id"]),
                        "base_effective_start": base_version["effective_start"],
                        "base_source_type": base_version["source_type"],
                        "base_charge_count": int(base_version["charge_count"] or 0),
                        "rider_family_key": rider_link["rider_family_key"],
                        "mandatory": int(rider_link["mandatory"] or 0),
                        "in_rider_summary": int(rider_link["in_rider_summary"] or 0),
                        "enrollment_type": rider_link["enrollment_type"],
                        "rider_status": rider_status,
                        "rider_version_id": selected_rider["version_id"] if selected_rider else None,
                        "rider_effective_start": selected_rider["effective_start"] if selected_rider else None,
                        "rider_source_type": selected_rider["source_type"] if selected_rider else None,
                        "rider_charge_count": selected_rider["charge_count"] if selected_rider else 0,
                    }
                )
    return rows


def _resolve_rider_status(
    conn: sqlite3.Connection,
    *,
    rider_family_key: str,
    ref_date: str,
) -> tuple[str, dict[str, Any] | None]:
    if not ref_date:
        return "base_version_missing_date", None
    first_rider_start = conn.execute(
        """
        SELECT MIN(tv.effective_start)
        FROM tariff_versions tv
        WHERE tv.family_key = ?
          AND tv.effective_start IS NOT NULL
        """,
        (rider_family_key,),
    ).fetchone()[0]
    if first_rider_start is not None and str(ref_date) < str(first_rider_start):
        return (
            "expected_before_rider_start",
            {
                "version_id": None,
                "effective_start": first_rider_start,
                "source_type": None,
                "charge_count": 0,
            },
        )
    selected = conn.execute(
        """
        SELECT
            tv.id AS version_id,
            tv.effective_start,
            tv.source_type,
            COUNT(tc.id) AS charge_count
        FROM tariff_versions tv
        LEFT JOIN tariff_charges tc
          ON tc.version_id = tv.id
        WHERE tv.family_key = ?
          AND tv.effective_start IS NOT NULL
          AND tv.effective_start <= ?
          AND (tv.effective_end IS NULL OR tv.effective_end >= ?)
        GROUP BY tv.id, tv.effective_start, tv.source_type
        ORDER BY tv.effective_start DESC, tv.id DESC
        LIMIT 1
        """,
        (rider_family_key, ref_date, ref_date),
    ).fetchone()
    if selected is None:
        return "no_active_rider_version", None
    payload = dict(selected)
    charge_count = int(payload["charge_count"] or 0)
    if charge_count <= 0:
        return "rider_version_zero_charges", payload
    if str(payload["effective_start"]) == ref_date:
        return "same_day_rider_version", payload
    return "carried_forward_rider_version", payload


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
    problem_rows = [
        row for row in rows
        if row["rider_status"] in {"no_active_rider_version", "rider_version_zero_charges"}
    ]
    lines = [
        "# DEP Residential Rider Gap Audit",
        "",
        f"Generated from SQLite on {report['generated_at']}.",
        "",
        f"- Base schedules audited: {report['base_schedule_count']}",
        f"- Base versions audited: {report['base_version_count']}",
        f"- Linked rider families audited: {report['linked_rider_family_count']}",
        "",
        "Status counts:",
    ]
    for status, count in dict(report["status_counts"]).items():
        lines.append(f"- `{status}`: {count}")
    lines.extend(
        [
            "",
            "## Rider-family gaps",
            "",
            _render_table(problem_rows[:40]),
            "",
        ]
    )
    return "\n".join(lines)


def _render_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_No rider-family gaps detected._"
    header = "Schedule    Base Start   Rider Key                     Status                     Rider Start   Charges"
    body = []
    for row in rows:
        body.append(
            f"{str(row['schedule_label']):<10}  "
            f"{str(row['base_effective_start'] or '-'): <10}  "
            f"{str(row['rider_family_key']):<28}  "
            f"{str(row['rider_status']):<25}  "
            f"{str(row['rider_effective_start'] or '-'): <10}  "
            f"{int(row['rider_charge_count'] or 0):>7}"
        )
    return "```text\n" + "\n".join([header, *body]) + "\n```"


__all__ = [
    "build_dep_residential_rider_gap_audit",
    "export_dep_residential_rider_gap_audit",
    "_DEFAULT_OUTPUT_DIR",
]
