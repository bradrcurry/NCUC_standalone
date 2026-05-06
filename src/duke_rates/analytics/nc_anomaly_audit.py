from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from duke_rates.analytics.nc_coverage_assessment import get_nc_coverage_families

_DEFAULT_OUTPUT_DIR = Path("docs/reports/nc_anomaly_audit")
_SOURCE_PRIORITY = {
    "historical_document": 5,
    "regulator": 4,
    "compliance_bundle": 3,
    "historical": 3,
    "utility_current": 2,
}
# (utility, schedule_label) pairs where demand charges are expected.
# Omit pairs confirmed energy-only to suppress false-positive missing_demand_rows flags:
#   DEP SGS / SGS-TOUE: tiered kWh + optional surcharge, no demand billing
#   DEC ES: Energy Star residential, energy-only (tiered kWh)
_DEMAND_EXPECTED: set[tuple[str, str]] = {
    ("DEP", "R-TOUD"),
    ("DEC", "SGS"),
    ("DEP", "LGS"),
    ("DEC", "LGS"),
    ("DEP", "LGS-TOU"),
    ("DEC", "LGS-TOU"),
    ("DEC", "I"),
    ("DEP", "I"),
}
# (utility, schedule_label) pairs confirmed to use flat (non-period-labeled) pricing
# despite having "TOU" in the schedule label — suppress false-positive missing_tou_structure flags.
#   DEP R-TOU-CPP: Critical Peak Pricing uses flat year-round on/off/critical_peak/discount
#                  pricing without per-season TOU period labels; 4 charges is correct.
_TOU_STRUCTURE_FLAT: set[tuple[str, str]] = {
    ("DEP", "R-TOU-CPP"),
}


@dataclass(frozen=True)
class FamilyContext:
    utility: str
    schedule_label: str
    family_key: str
    full_threshold: int
    partial_threshold: int


def _connect(database_path: Path | None = None) -> sqlite3.Connection:
    path = Path(database_path or "data/db/duke_rates.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def build_nc_anomaly_audit(
    database_path: Path | None = None,
    *,
    include_only_flagged: bool = True,
) -> dict[str, object]:
    family_contexts = _family_contexts()
    conn = _connect(database_path)
    try:
        version_rows = _load_version_rows(conn, tuple(family_contexts))
    finally:
        conn.close()

    family_peak_charge_count = {
        family_key: max(int(row["charge_count"] or 0) for row in rows)
        for family_key, rows in _group_by_family(version_rows).items()
    }
    duplicate_same_start_counts = Counter(
        (str(row["family_key"]), str(row["effective_start"] or ""))
        for row in version_rows
        if row["effective_start"]
    )

    anomaly_rows: list[dict[str, object]] = []
    clean_rows: list[dict[str, object]] = []
    for row in version_rows:
        context = family_contexts[str(row["family_key"])]
        findings = _detect_findings(
            row,
            context=context,
            family_peak_charge_count=family_peak_charge_count.get(str(row["family_key"]), 0),
            duplicate_same_start_counts=duplicate_same_start_counts,
        )
        if findings:
            anomaly_rows.extend(findings)
        else:
            clean_rows.append(_base_row(row, context=context, anomaly_type="ok"))

    anomaly_rows.sort(
        key=lambda item: (
            int(item["severity_score"]),
            int(item["charge_count"]) == 0,
            str(item["family_key"]),
            str(item["effective_start"] or ""),
            str(item["anomaly_type"]),
        ),
        reverse=True,
    )

    action_counts = Counter(str(row["recommended_action"]) for row in anomaly_rows)
    anomaly_type_counts = Counter(str(row["anomaly_type"]) for row in anomaly_rows)
    family_counts = Counter(str(row["family_key"]) for row in anomaly_rows)
    summary_rows = anomaly_rows if include_only_flagged else [*anomaly_rows, *clean_rows]
    return {
        "generated_at": date.today().isoformat(),
        "total_versions_scanned": len(version_rows),
        "flagged_versions": len({int(row["version_id"]) for row in anomaly_rows}),
        "total_anomalies": len(anomaly_rows),
        "anomaly_type_counts": dict(sorted(anomaly_type_counts.items())),
        "recommended_action_counts": dict(sorted(action_counts.items())),
        "family_flag_counts": dict(sorted(family_counts.items())),
        "rows": summary_rows,
    }


def export_nc_anomaly_audit(
    output_dir: Path,
    *,
    database_path: Path | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_nc_anomaly_audit(database_path)

    rows_csv = output_dir / "nc_anomaly_audit_rows.csv"
    summary_json = output_dir / "nc_anomaly_audit_summary.json"
    markdown_path = output_dir / "nc_anomaly_audit.md"

    _write_csv(rows_csv, report["rows"])
    summary_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")

    return {
        "rows_csv": rows_csv,
        "summary_json": summary_json,
        "markdown": markdown_path,
    }


def _family_contexts() -> dict[str, FamilyContext]:
    contexts: dict[str, FamilyContext] = {}
    families = get_nc_coverage_families()
    for utility_key in ("dep", "dec"):
        for family in families[utility_key]:
            contexts[family.family_key] = FamilyContext(
                utility=family.utility,
                schedule_label=family.label,
                family_key=family.family_key,
                full_threshold=family.full_threshold,
                partial_threshold=family.partial_threshold,
            )
    return contexts


def _load_version_rows(
    conn: sqlite3.Connection,
    family_keys: tuple[str, ...],
) -> list[sqlite3.Row]:
    placeholders = ", ".join("?" for _ in family_keys)
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
        latest_queue AS (
            SELECT hrq.*
            FROM historical_reprocess_queue hrq
            INNER JOIN (
                SELECT historical_document_id, MAX(id) AS max_id
                FROM historical_reprocess_queue
                GROUP BY historical_document_id
            ) latest
              ON latest.max_id = hrq.id
        )
        SELECT
            tv.id AS version_id,
            tv.family_key,
            tv.effective_start,
            tv.effective_end,
            tv.revision_label,
            tv.source_type,
            tv.source_pdf,
            tv.historical_document_id,
            COALESCE(vcs.charge_count, 0) AS charge_count,
            COALESCE(vcs.null_rate_count, 0) AS null_rate_count,
            COUNT(DISTINCT tc.charge_type) AS charge_type_count,
            COUNT(DISTINCT CASE WHEN COALESCE(tc.season, '') <> '' THEN tc.season END) AS season_label_count,
            COUNT(DISTINCT CASE WHEN COALESCE(tc.tou_period, '') <> '' THEN tc.tou_period END) AS tou_period_count,
            COUNT(DISTINCT CASE WHEN COALESCE(tc.customer_class, '') <> '' THEN tc.customer_class END) AS customer_class_count,
            SUM(CASE WHEN tc.charge_type = 'fixed' THEN 1 ELSE 0 END) AS fixed_count,
            SUM(CASE WHEN tc.charge_type = 'demand' THEN 1 ELSE 0 END) AS demand_count,
            SUM(CASE WHEN tc.charge_type IN ('energy_block', 'tou_energy') THEN 1 ELSE 0 END) AS energy_count,
            hd.title AS historical_title,
            hd.local_path AS historical_local_path,
            lr.status AS latest_run_status,
            lr.outcome_quality AS latest_outcome_quality,
            lr.charge_count AS latest_run_charge_count,
            lr.parser_profile AS latest_parser_profile,
            lq.status AS reprocess_status,
            lq.queue_reason AS reprocess_queue_reason,
            lq.priority AS reprocess_priority
        FROM tariff_versions tv
        LEFT JOIN v_version_charge_summary vcs
          ON vcs.version_id = tv.id
        LEFT JOIN tariff_charges tc
          ON tc.version_id = tv.id
        LEFT JOIN historical_documents hd
          ON hd.id = tv.historical_document_id
        LEFT JOIN latest_runs lr
          ON lr.historical_document_id = tv.historical_document_id
        LEFT JOIN latest_queue lq
          ON lq.historical_document_id = tv.historical_document_id
        WHERE tv.family_key IN ({placeholders})
        GROUP BY
            tv.id,
            tv.family_key,
            tv.effective_start,
            tv.effective_end,
            tv.revision_label,
            tv.source_type,
            tv.source_pdf,
            tv.historical_document_id,
            vcs.charge_count,
            vcs.null_rate_count,
            hd.title,
            hd.local_path,
            lr.status,
            lr.outcome_quality,
            lr.charge_count,
            lr.parser_profile,
            lq.status,
            lq.queue_reason,
            lq.priority
        ORDER BY tv.family_key, tv.effective_start, tv.id
    """
    return list(conn.execute(query, family_keys).fetchall())


def _group_by_family(rows: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["family_key"]), []).append(row)
    return grouped


def _detect_findings(
    row: sqlite3.Row,
    *,
    context: FamilyContext,
    family_peak_charge_count: int,
    duplicate_same_start_counts: Counter[tuple[str, str]],
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    charge_count = int(row["charge_count"] or 0)
    null_rate_count = int(row["null_rate_count"] or 0)
    tou_period_count = int(row["tou_period_count"] or 0)
    demand_count = int(row["demand_count"] or 0)
    energy_count = int(row["energy_count"] or 0)
    latest_outcome_quality = str(row["latest_outcome_quality"] or "")
    duplicate_count = duplicate_same_start_counts.get(
        (str(row["family_key"]), str(row["effective_start"] or "")),
        0,
    )
    peak_ratio = round(charge_count / family_peak_charge_count, 3) if family_peak_charge_count else 0.0

    if charge_count == 0:
        findings.append(
            _base_row(
                row,
                context=context,
                anomaly_type="zero_charge_version",
                severity_score=95 if row["historical_document_id"] else 80,
                peak_ratio=peak_ratio,
                recommended_action=_queue_aware_action(row, default_action="reparse_with_updated_profile"),
                reason="Version exists in the tariff timeline but has zero extracted charges.",
            )
        )

    if null_rate_count > 0:
        findings.append(
            _base_row(
                row,
                context=context,
                anomaly_type="null_rate_rows",
                severity_score=72,
                peak_ratio=peak_ratio,
                recommended_action="inspect_source_pdf",
                reason=f"Version contains {null_rate_count} charge rows with null rate values.",
            )
        )

    if (
        charge_count > 0
        and family_peak_charge_count >= max(context.partial_threshold, 4)
        and charge_count < context.partial_threshold
        and peak_ratio <= 0.4
    ):
        findings.append(
            _base_row(
                row,
                context=context,
                anomaly_type="sparse_vs_family_peak",
                severity_score=78,
                peak_ratio=peak_ratio,
                recommended_action=_queue_aware_action(row, default_action="inspect_source_pdf"),
                reason=(
                    f"Version has only {charge_count} extracted charges versus a family peak of "
                    f"{family_peak_charge_count}."
                ),
            )
        )

    if (
        "TOU" in context.schedule_label
        and charge_count > 0
        and tou_period_count == 0
        and (context.utility, context.schedule_label) not in _TOU_STRUCTURE_FLAT
    ):
        findings.append(
            _base_row(
                row,
                context=context,
                anomaly_type="missing_tou_structure",
                severity_score=82,
                peak_ratio=peak_ratio,
                recommended_action=_queue_aware_action(row, default_action="reparse_with_updated_profile"),
                reason="TOU schedule has extracted rows but no populated TOU period labels.",
            )
        )

    if (
        (context.utility, context.schedule_label) in _DEMAND_EXPECTED
        and charge_count > 0
        and demand_count == 0
        and (charge_count >= 2 or energy_count > 0)
    ):
        findings.append(
            _base_row(
                row,
                context=context,
                anomaly_type="missing_demand_rows",
                severity_score=76,
                peak_ratio=peak_ratio,
                recommended_action=_queue_aware_action(row, default_action="reparse_with_updated_profile"),
                reason="Schedule family normally includes demand rows, but none were extracted.",
            )
        )

    if duplicate_count > 1:
        findings.append(
            _base_row(
                row,
                context=context,
                anomaly_type="duplicate_same_start_versions",
                severity_score=68 + min(duplicate_count, 4),
                peak_ratio=peak_ratio,
                recommended_action="review_duplicate_versions",
                reason=(
                    f"Family has {duplicate_count} versions sharing effective_start="
                    f"{row['effective_start']}."
                ),
            )
        )

    if latest_outcome_quality in {"empty", "weak"}:
        findings.append(
            _base_row(
                row,
                context=context,
                anomaly_type="weak_latest_parse",
                severity_score=88 if latest_outcome_quality == "empty" else 74,
                peak_ratio=peak_ratio,
                recommended_action=_queue_aware_action(row, default_action="reparse_with_updated_profile"),
                reason=f"Latest historical processing run is marked {latest_outcome_quality}.",
            )
        )

    return findings


def _queue_aware_action(row: sqlite3.Row, *, default_action: str) -> str:
    reprocess_status = str(row["reprocess_status"] or "")
    if reprocess_status in {"pending", "running"}:
        return "process_reprocess_queue"
    return default_action


def _base_row(
    row: sqlite3.Row,
    *,
    context: FamilyContext,
    anomaly_type: str,
    severity_score: int = 0,
    peak_ratio: float | None = None,
    recommended_action: str | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    return {
        "utility": context.utility,
        "schedule_label": context.schedule_label,
        "family_key": context.family_key,
        "version_id": int(row["version_id"]),
        "effective_start": row["effective_start"],
        "effective_end": row["effective_end"],
        "revision_label": row["revision_label"],
        "source_type": row["source_type"],
        "source_priority": _SOURCE_PRIORITY.get(str(row["source_type"] or "").lower(), 0),
        "historical_document_id": row["historical_document_id"],
        "historical_title": row["historical_title"],
        "historical_local_path": row["historical_local_path"],
        "source_pdf": row["source_pdf"],
        "charge_count": int(row["charge_count"] or 0),
        "null_rate_count": int(row["null_rate_count"] or 0),
        "charge_type_count": int(row["charge_type_count"] or 0),
        "season_label_count": int(row["season_label_count"] or 0),
        "tou_period_count": int(row["tou_period_count"] or 0),
        "customer_class_count": int(row["customer_class_count"] or 0),
        "fixed_count": int(row["fixed_count"] or 0),
        "demand_count": int(row["demand_count"] or 0),
        "energy_count": int(row["energy_count"] or 0),
        "peak_ratio": round(peak_ratio or 0.0, 3),
        "latest_run_status": row["latest_run_status"],
        "latest_outcome_quality": row["latest_outcome_quality"],
        "latest_run_charge_count": row["latest_run_charge_count"],
        "latest_parser_profile": row["latest_parser_profile"],
        "reprocess_status": row["reprocess_status"],
        "reprocess_queue_reason": row["reprocess_queue_reason"],
        "reprocess_priority": row["reprocess_priority"],
        "anomaly_type": anomaly_type,
        "severity_score": severity_score,
        "recommended_action": recommended_action,
        "reason": reason,
    }


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
    top_rows = rows[:25]
    lines = [
        "# NC Anomaly Audit",
        "",
        f"Generated from SQLite on {report['generated_at']}.",
        "",
        f"- Versions scanned: {report['total_versions_scanned']}",
        f"- Flagged versions: {report['flagged_versions']}",
        f"- Total anomaly rows: {report['total_anomalies']}",
        "",
        "Recommended action counts:",
    ]
    for action, count in dict(report["recommended_action_counts"]).items():  # type: ignore[arg-type]
        lines.append(f"- `{action}`: {count}")
    lines.extend(["", "Anomaly type counts:"])
    for anomaly_type, count in dict(report["anomaly_type_counts"]).items():  # type: ignore[arg-type]
        lines.append(f"- `{anomaly_type}`: {count}")
    lines.extend(["", "## Top flagged rows", "", _render_table(top_rows), ""])
    return "\n".join(lines)


def _render_table(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "_No anomalies detected._"
    header = "Score  Utility  Schedule    Start       Type                         Action                     Charges  Queue"
    body = []
    for row in rows:
        body.append(
            f"{int(row['severity_score']):>5}  "
            f"{str(row['utility']):<7}  "
            f"{str(row['schedule_label']):<10}  "
            f"{str(row['effective_start'] or '-'): <10}  "
            f"{str(row['anomaly_type']):<27}  "
            f"{str(row['recommended_action'] or '-'): <25}  "
            f"{int(row['charge_count']):>7}  "
            f"{str(row['reprocess_status'] or '-')}"
        )
    return "```text\n" + "\n".join([header, *body]) + "\n```"


__all__ = [
    "build_nc_anomaly_audit",
    "export_nc_anomaly_audit",
    "_DEFAULT_OUTPUT_DIR",
]
