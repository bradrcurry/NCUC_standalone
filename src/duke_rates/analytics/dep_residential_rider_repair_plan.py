from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from duke_rates.analytics.dep_residential_rider_action_queue import (
    build_dep_residential_rider_action_queue,
)

_DEFAULT_OUTPUT_DIR = Path("docs/reports/dep_residential_rider_repair_plan")
_RIDER_HINTS: dict[str, dict[str, str]] = {
    "nc-progress-leaf-604": {
        "rider_label": "EDIT-4",
        "parser_profile": "progress_single_value_rider",
        "search_hint": 'E-2 Sub 1196 | "EDIT-4" | "Excess Deferred" | "Leaf No. 604"',
        "discovery_note": "Historical 2016-2020 annual versions are expected from E-2 Sub 1196.",
    },
    "nc-progress-leaf-605": {
        "rider_label": "CPRE",
        "parser_profile": "progress_single_value_rider",
        "search_hint": 'E-2 Sub 1109 | "CPRE" | "Competitive Procurement" | "Leaf No. 605"',
        "discovery_note": "Existing historical documents are linked, but many runs are empty or skipped.",
    },
    "nc-progress-leaf-608": {
        "rider_label": "RDM",
        "parser_profile": "progress_single_value_rider",
        "search_hint": 'E-2 Sub 1294 | "Rider RDM" | "Revenue Decoupling" | "Leaf No. 608"',
        "discovery_note": "Historical 2015-2022 coverage appears largely absent from linked versions.",
    },
    "nc-progress-leaf-609": {
        "rider_label": "ESM",
        "parser_profile": "progress_single_value_rider",
        "search_hint": 'E-2 annual compliance | "Rider ESM" | "Earnings Sharing Mechanism" | "Leaf No. 609"',
        "discovery_note": "Current linked versions are empty; earlier rider history is mostly missing.",
    },
    "nc-progress-leaf-610": {
        "rider_label": "PIM",
        "parser_profile": "progress_single_value_rider",
        "search_hint": 'E-2 Sub 1108 | "Rider PIM" | "Performance Incentive" | "Leaf No. 610"',
        "discovery_note": "Historical documents exist, but several versions are empty or skipped.",
    },
    "nc-progress-leaf-611": {
        "rider_label": "CAR",
        "parser_profile": "progress_customer_assistance_recovery",
        "search_hint": 'E-2 Sub 1252 | "Rider CAR" | "Customer Assistance Recovery" | "Leaf No. 611"',
        "discovery_note": "Family has a dedicated parser profile; empty historical spans should be reparsed first.",
    },
}


def _connect(database_path: Path | None = None) -> sqlite3.Connection:
    path = Path(database_path or "data/db/duke_rates.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def build_dep_residential_rider_repair_plan(
    database_path: Path | None = None,
) -> dict[str, Any]:
    action_queue = build_dep_residential_rider_action_queue(database_path)
    conn = _connect(database_path)
    try:
        historical_stats = _load_historical_stats(conn, tuple(_RIDER_HINTS))
    finally:
        conn.close()

    grouped_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in action_queue["rows"]:
        grouped_rows[str(row["rider_family_key"])].append(row)

    plan_rows: list[dict[str, Any]] = []
    for rider_family_key, rows in grouped_rows.items():
        hint = _RIDER_HINTS.get(rider_family_key)
        if hint is None:
            continue
        stats = historical_stats.get(rider_family_key, {})
        total_priority = sum(int(row["priority_score"]) for row in rows)
        top_action = str(rows[0]["recommended_action"])
        plan_rows.append(
            {
                "priority_score": total_priority,
                "priority_band": _priority_band(total_priority),
                "rider_family_key": rider_family_key,
                "rider_label": hint["rider_label"],
                "recommended_action": top_action,
                "parser_profile": hint["parser_profile"],
                "affected_schedule_count": len({str(row["base_family_key"]) for row in rows}),
                "affected_schedules": ",".join(sorted({str(row["schedule_label"]) for row in rows})),
                "action_item_count": len(rows),
                "historical_version_count": int(stats.get("historical_version_count", 0)),
                "historical_doc_count": int(stats.get("historical_doc_count", 0)),
                "strong_run_count": int(stats.get("strong_run_count", 0)),
                "empty_run_count": int(stats.get("empty_run_count", 0)),
                "skipped_run_count": int(stats.get("skipped_run_count", 0)),
                "zero_charge_version_count": int(stats.get("zero_charge_version_count", 0)),
                "command_hint": _command_hint(top_action, hint["parser_profile"], rider_family_key),
                "search_hint": hint["search_hint"],
                "discovery_note": hint["discovery_note"],
            }
        )

    plan_rows.sort(
        key=lambda row: (
            int(row["priority_score"]),
            int(row["affected_schedule_count"]),
            str(row["rider_family_key"]),
        ),
        reverse=True,
    )
    return {
        "generated_at": date.today().isoformat(),
        "rider_family_count": len(plan_rows),
        "recommended_action_counts": dict(
            sorted(Counter(str(row["recommended_action"]) for row in plan_rows).items())
        ),
        "rows": plan_rows,
    }


def export_dep_residential_rider_repair_plan(
    output_dir: Path,
    *,
    database_path: Path | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_dep_residential_rider_repair_plan(database_path)

    rows_csv = output_dir / "dep_residential_rider_repair_plan_rows.csv"
    summary_json = output_dir / "dep_residential_rider_repair_plan_summary.json"
    markdown_path = output_dir / "dep_residential_rider_repair_plan.md"

    _write_csv(rows_csv, report["rows"])
    summary_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")

    return {
        "rows_csv": rows_csv,
        "summary_json": summary_json,
        "markdown": markdown_path,
    }


def _load_historical_stats(
    conn: sqlite3.Connection,
    rider_family_keys: tuple[str, ...],
) -> dict[str, dict[str, int]]:
    placeholders = ", ".join("?" for _ in rider_family_keys)
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
        )
        SELECT
            tv.family_key,
            COUNT(DISTINCT tv.id) AS historical_version_count,
            COUNT(DISTINCT tv.historical_document_id) AS historical_doc_count,
            SUM(CASE WHEN COALESCE(vcs.charge_count, 0) = 0 THEN 1 ELSE 0 END) AS zero_charge_version_count,
            SUM(CASE WHEN lr.outcome_quality = 'strong' THEN 1 ELSE 0 END) AS strong_run_count,
            SUM(CASE WHEN lr.outcome_quality = 'empty' THEN 1 ELSE 0 END) AS empty_run_count,
            SUM(CASE WHEN lr.outcome_quality = 'skipped' THEN 1 ELSE 0 END) AS skipped_run_count
        FROM tariff_versions tv
        LEFT JOIN v_version_charge_summary vcs
          ON vcs.version_id = tv.id
        LEFT JOIN latest_runs lr
          ON lr.historical_document_id = tv.historical_document_id
        WHERE tv.family_key IN ({placeholders})
          AND tv.source_type <> 'utility_current'
        GROUP BY tv.family_key
    """
    rows = conn.execute(query, rider_family_keys).fetchall()
    return {str(row["family_key"]): dict(row) for row in rows}


def _priority_band(priority_score: int) -> str:
    if priority_score >= 180:
        return "high"
    if priority_score >= 60:
        return "medium"
    return "low"


def _command_hint(recommended_action: str, parser_profile: str, rider_family_key: str) -> str:
    if recommended_action == "identify_or_link_missing_rider_documents":
        return f"python -m duke_rates reprocess show-profile-impact-nc --parser-profile {parser_profile} --family-key {rider_family_key}"
    return (
        f"python -m duke_rates reprocess enqueue-profile-impact-nc --parser-profile {parser_profile} "
        f"--family-key {rider_family_key}"
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
        "# DEP Residential Rider Repair Plan",
        "",
        f"Generated from SQLite on {report['generated_at']}.",
        "",
        f"- Rider families ranked: {report['rider_family_count']}",
        "",
        "Recommended action counts:",
    ]
    for action, count in dict(report["recommended_action_counts"]).items():
        lines.append(f"- `{action}`: {count}")
    lines.extend(["", "## Ranked Rider Families", "", _render_table(rows), ""])
    return "\n".join(lines)


def _render_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_No repair items detected._"
    header = "Score  Band    Rider   Action                                 Schedules  Docs  Empty  Skipped  Zero  Profile"
    body = []
    for row in rows:
        body.append(
            f"{int(row['priority_score']):>5}  "
            f"{str(row['priority_band']):<6}  "
            f"{str(row['rider_label']):<6}  "
            f"{str(row['recommended_action']):<37}  "
            f"{int(row['affected_schedule_count']):>9}  "
            f"{int(row['historical_doc_count']):>4}  "
            f"{int(row['empty_run_count']):>5}  "
            f"{int(row['skipped_run_count']):>7}  "
            f"{int(row['zero_charge_version_count']):>4}  "
            f"{str(row['parser_profile'])}"
        )
    return "```text\n" + "\n".join([header, *body]) + "\n```"


__all__ = [
    "build_dep_residential_rider_repair_plan",
    "export_dep_residential_rider_repair_plan",
    "_DEFAULT_OUTPUT_DIR",
]
