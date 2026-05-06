from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from duke_rates.analytics.dep_residential_rider_gap_audit import (
    build_dep_residential_rider_gap_audit,
)

_DEFAULT_OUTPUT_DIR = Path("docs/reports/dep_residential_rider_action_queue")
_PROBLEM_STATUSES = {"no_active_rider_version", "rider_version_zero_charges"}
_STATUS_WEIGHTS = {
    "no_active_rider_version": 9,
    "rider_version_zero_charges": 7,
    "same_day_rider_version": 0,
    "carried_forward_rider_version": 0,
    "expected_before_rider_start": 0,
    "base_version_missing_date": 1,
}


def build_dep_residential_rider_action_queue(
    database_path: Path | None = None,
) -> dict[str, Any]:
    gap_report = build_dep_residential_rider_gap_audit(database_path)
    gap_rows = list(gap_report["rows"])
    action_rows = _build_action_rows(gap_rows)
    return {
        "generated_at": date.today().isoformat(),
        "linked_rider_family_count": len({row["rider_family_key"] for row in gap_rows}),
        "base_schedule_count": len({row["base_family_key"] for row in gap_rows}),
        "base_version_count": len({(row["base_family_key"], row["base_version_id"]) for row in gap_rows}),
        "problem_row_count": len([row for row in gap_rows if row["rider_status"] in _PROBLEM_STATUSES]),
        "action_item_count": len(action_rows),
        "recommended_action_counts": dict(
            sorted(Counter(str(row["recommended_action"]) for row in action_rows).items())
        ),
        "rows": action_rows,
    }


def export_dep_residential_rider_action_queue(
    output_dir: Path,
    *,
    database_path: Path | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_dep_residential_rider_action_queue(database_path)

    rows_csv = output_dir / "dep_residential_rider_action_queue_rows.csv"
    summary_json = output_dir / "dep_residential_rider_action_queue_summary.json"
    markdown_path = output_dir / "dep_residential_rider_action_queue.md"

    _write_csv(rows_csv, report["rows"])
    summary_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")

    return {
        "rows_csv": rows_csv,
        "summary_json": summary_json,
        "markdown": markdown_path,
    }


def _build_action_rows(gap_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in gap_rows:
        grouped[(str(row["base_family_key"]), str(row["rider_family_key"]))].append(row)

    action_rows: list[dict[str, Any]] = []
    for (base_family_key, rider_family_key), rows in grouped.items():
        status_counts = Counter(str(row["rider_status"]) for row in rows)
        if not any(status in _PROBLEM_STATUSES for status in status_counts):
            continue

        schedule_label = str(rows[0]["schedule_label"])
        severity_score = _severity_score(rows, status_counts)
        recommended_action = _recommended_action(status_counts)
        base_starts = sorted(str(row["base_effective_start"] or "") for row in rows if row["base_effective_start"])
        action_rows.append(
            {
                "priority_score": severity_score,
                "priority_band": _priority_band(severity_score),
                "recommended_action": recommended_action,
                "schedule_label": schedule_label,
                "base_family_key": base_family_key,
                "rider_family_key": rider_family_key,
                "affected_base_versions": len({int(row["base_version_id"]) for row in rows}),
                "first_affected_base_start": base_starts[0] if base_starts else None,
                "latest_affected_base_start": base_starts[-1] if base_starts else None,
                "no_active_rider_version_count": status_counts.get("no_active_rider_version", 0),
                "rider_version_zero_charges_count": status_counts.get("rider_version_zero_charges", 0),
                "carried_forward_count": status_counts.get("carried_forward_rider_version", 0),
                "same_day_count": status_counts.get("same_day_rider_version", 0),
                "problem_ratio": _format_ratio(
                    status_counts.get("no_active_rider_version", 0)
                    + status_counts.get("rider_version_zero_charges", 0),
                    len(rows),
                ),
                "notes": _build_notes(schedule_label, rider_family_key, status_counts, rows),
            }
        )

    action_rows.sort(
        key=lambda row: (
            int(row["priority_score"]),
            int(row["affected_base_versions"]),
            str(row["base_family_key"]),
            str(row["rider_family_key"]),
        ),
        reverse=True,
    )
    return action_rows


def _severity_score(rows: list[dict[str, Any]], status_counts: Counter[str]) -> int:
    score = sum(_STATUS_WEIGHTS.get(str(row["rider_status"]), 0) for row in rows)
    if status_counts.get("no_active_rider_version", 0) and status_counts.get("rider_version_zero_charges", 0):
        score += 6
    if status_counts.get("carried_forward_rider_version", 0) == 0 and status_counts.get("same_day_rider_version", 0) == 0:
        score += 4
    if any(int(row.get("mandatory") or 0) for row in rows):
        score += 3
    if any(int(row.get("in_rider_summary") or 0) for row in rows):
        score += 2
    return score


def _recommended_action(status_counts: Counter[str]) -> str:
    no_active = status_counts.get("no_active_rider_version", 0)
    zero_charge = status_counts.get("rider_version_zero_charges", 0)
    if no_active and zero_charge:
        return "backfill_documents_then_reparse_rider_family"
    if no_active:
        return "identify_or_link_missing_rider_documents"
    return "reparse_existing_rider_versions"


def _priority_band(priority_score: int) -> str:
    if priority_score >= 80:
        return "high"
    if priority_score >= 35:
        return "medium"
    return "low"


def _format_ratio(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0.0%"
    return f"{(100.0 * numerator) / denominator:.1f}%"


def _build_notes(
    schedule_label: str,
    rider_family_key: str,
    status_counts: Counter[str],
    rows: list[dict[str, Any]],
) -> str:
    fragments = [f"{schedule_label} -> {rider_family_key}"]
    if status_counts.get("no_active_rider_version", 0):
        fragments.append(
            f"missing active rider versions in {status_counts['no_active_rider_version']} base windows"
        )
    if status_counts.get("rider_version_zero_charges", 0):
        fragments.append(
            f"zero-charge rider versions in {status_counts['rider_version_zero_charges']} base windows"
        )
    if status_counts.get("carried_forward_rider_version", 0):
        fragments.append(
            f"carry-forward succeeds in {status_counts['carried_forward_rider_version']} windows"
        )
    if status_counts.get("same_day_rider_version", 0):
        fragments.append(f"same-day rider coverage in {status_counts['same_day_rider_version']} windows")
    if status_counts.get("expected_before_rider_start", 0):
        fragments.append(
            f"rider not expected yet in {status_counts['expected_before_rider_start']} earlier base windows"
        )
    charged_starts = sorted(
        {
            str(row["rider_effective_start"])
            for row in rows
            if row["rider_status"]
            in {"same_day_rider_version", "carried_forward_rider_version", "expected_before_rider_start"}
            and row["rider_effective_start"]
        }
    )
    if charged_starts:
        fragments.append(f"charged rider history seen from {charged_starts[0]}")
    return "; ".join(fragments)


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
        "# DEP Residential Rider Action Queue",
        "",
        f"Generated from SQLite on {report['generated_at']}.",
        "",
        f"- Base schedules covered: {report['base_schedule_count']}",
        f"- Base versions covered: {report['base_version_count']}",
        f"- Linked rider families covered: {report['linked_rider_family_count']}",
        f"- Problematic rider/base rows in source audit: {report['problem_row_count']}",
        f"- Action items: {report['action_item_count']}",
        "",
        "Recommended action counts:",
    ]
    for action, count in dict(report["recommended_action_counts"]).items():
        lines.append(f"- `{action}`: {count}")
    lines.extend(
        [
            "",
            "## Ranked Queue",
            "",
            _render_table(rows[:40]),
            "",
        ]
    )
    return "\n".join(lines)


def _render_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_No action items detected._"
    header = "Score  Band    Action                                 Schedule    Rider Key                     Affected  Problem%  First       Latest"
    body = []
    for row in rows:
        body.append(
            f"{int(row['priority_score']):>5}  "
            f"{str(row['priority_band']):<6}  "
            f"{str(row['recommended_action']):<37}  "
            f"{str(row['schedule_label']):<10}  "
            f"{str(row['rider_family_key']):<28}  "
            f"{int(row['affected_base_versions']):>8}  "
            f"{str(row['problem_ratio']):>8}  "
            f"{str(row['first_affected_base_start'] or '-'): <10}  "
            f"{str(row['latest_affected_base_start'] or '-'): <10}"
        )
    return "```text\n" + "\n".join([header, *body]) + "\n```"


__all__ = [
    "build_dep_residential_rider_action_queue",
    "export_dep_residential_rider_action_queue",
    "_DEFAULT_OUTPUT_DIR",
]
