from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from duke_rates.analytics.bill_rider_date_audit import build_progress_nc_bill_rider_date_audit
from duke_rates.analytics.dep_progress import _connect, _require_pandas
from duke_rates.config import get_settings
from duke_rates.db.repository import Repository


def load_dep_rider_date_audit_report(
    *,
    database_path: Path | None = None,
    usage_xml_path: Path | None = None,
) -> dict[str, Any]:
    pd = _require_pandas()
    settings = get_settings()
    database_path = database_path or settings.database_path
    usage_xml_path = usage_xml_path or Path(r"C:\Python\Duke\Standalone\data\usage\Energy Usage.xml")

    with _connect(database_path) as conn:
        component_completeness = pd.read_sql_query(
            """
            SELECT
                li.rider_code,
                COUNT(*) AS total_rows,
                SUM(
                    CASE
                        WHEN li.line_effective_date IS NOT NULL AND li.line_effective_date <> ''
                        THEN 1 ELSE 0
                    END
                ) AS dated_rows,
                SUM(
                    CASE
                        WHEN li.line_effective_date IS NULL OR li.line_effective_date = ''
                        THEN 1 ELSE 0
                    END
                ) AS undated_rows,
                GROUP_CONCAT(DISTINCT li.line_effective_date) AS distinct_effective_dates
            FROM rider_summary_blocks b
            JOIN rider_line_items li ON li.block_id = b.id
            WHERE b.applicable_schedules_json LIKE '%RES%'
              AND li.rider_code IS NOT NULL
              AND li.is_section_header = 0
              AND li.is_subtotal = 0
              AND li.is_total = 0
            GROUP BY li.rider_code
            ORDER BY li.rider_code
            """,
            conn,
        )
        component_date_matrix = pd.read_sql_query(
            """
            WITH uniq AS (
                SELECT DISTINCT
                    b.effective_date AS block_effective_date,
                    li.rider_code,
                    li.line_effective_date
                FROM rider_summary_blocks b
                JOIN rider_line_items li ON li.block_id = b.id
                WHERE b.applicable_schedules_json LIKE '%RES%'
                  AND li.rider_code IS NOT NULL
                  AND li.is_section_header = 0
                  AND li.is_subtotal = 0
                  AND li.is_total = 0
            )
            SELECT
                block_effective_date,
                rider_code,
                GROUP_CONCAT(DISTINCT line_effective_date) AS component_effective_dates
            FROM uniq
            GROUP BY block_effective_date, rider_code
            ORDER BY block_effective_date, rider_code
            """,
            conn,
        )
        suspect_component_rows = pd.read_sql_query(
            """
            SELECT
                b.id AS block_id,
                b.effective_date AS block_effective_date,
                li.rider_code,
                li.label,
                li.line_effective_date,
                li.cents_per_kwh,
                b.source_pdf,
                b.docket_dir
            FROM rider_summary_blocks b
            JOIN rider_line_items li ON li.block_id = b.id
            WHERE b.applicable_schedules_json LIKE '%RES%'
              AND li.rider_code IS NOT NULL
              AND li.is_section_header = 0
              AND li.is_subtotal = 0
              AND li.is_total = 0
              AND (li.line_effective_date IS NULL OR li.line_effective_date = '')
            ORDER BY b.effective_date, li.rider_code, b.id
            """,
            conn,
        )

    component_completeness["date_completeness_pct"] = (
        component_completeness["dated_rows"] / component_completeness["total_rows"] * 100.0
    ).round(1)
    component_completeness["tracking_status"] = component_completeness.apply(
        lambda row: _tracking_status_for_component(
            rider_code=str(row["rider_code"]),
            undated_rows=int(row["undated_rows"]),
        ),
        axis=1,
    )
    component_completeness["distinct_effective_date_count"] = component_completeness[
        "distinct_effective_dates"
    ].map(_csv_count)

    component_date_matrix["component_effective_date_count"] = component_date_matrix[
        "component_effective_dates"
    ].map(_csv_count)
    component_date_matrix["has_undated_rows"] = component_date_matrix["component_effective_dates"].isna()
    component_date_matrix["tracking_status"] = component_date_matrix.apply(
        lambda row: _tracking_status_for_matrix_row(
            rider_code=str(row["rider_code"]),
            has_undated_rows=bool(row["has_undated_rows"]),
        ),
        axis=1,
    )

    repository = Repository(database_path)
    bill_rows, bill_summary = build_progress_nc_bill_rider_date_audit(
        repository=repository,
        usage_xml_path=usage_xml_path,
    )
    bill_validation = pd.DataFrame(bill_rows)

    bill_assessment_summary = (
        bill_validation.groupby(["component_key", "assessment"]).size().reset_index(name="row_count")
        if not bill_validation.empty
        else pd.DataFrame(columns=["component_key", "assessment", "row_count"])
    )

    direct_rider_family_audit = _load_direct_rider_family_audit(database_path)

    summary = _build_summary(
        component_completeness=component_completeness,
        suspect_component_rows=suspect_component_rows,
        bill_summary=bill_summary,
        bill_assessment_summary=bill_assessment_summary,
        direct_rider_family_audit=direct_rider_family_audit,
    )

    return {
        "summary": summary,
        "component_completeness": component_completeness,
        "component_date_matrix": component_date_matrix,
        "suspect_component_rows": suspect_component_rows,
        "bill_assessment_summary": bill_assessment_summary,
        "bill_validation_rows": bill_validation,
        "direct_rider_family_audit": direct_rider_family_audit,
    }


def export_dep_rider_date_audit_report(
    output_dir: Path,
    *,
    database_path: Path | None = None,
    usage_xml_path: Path | None = None,
) -> dict[str, Path]:
    pd = _require_pandas()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = load_dep_rider_date_audit_report(
        database_path=database_path,
        usage_xml_path=usage_xml_path,
    )

    paths = {
        "summary_json": output_dir / "dep_res_rider_date_audit_summary.json",
        "component_completeness_csv": output_dir / "dep_res_rider_component_date_completeness.csv",
        "component_date_matrix_csv": output_dir / "dep_res_rider_component_date_matrix.csv",
        "suspect_rows_csv": output_dir / "dep_res_rider_component_suspect_rows.csv",
        "bill_assessment_summary_csv": output_dir / "dep_res_rider_bill_assessment_summary.csv",
        "bill_validation_rows_csv": output_dir / "dep_res_rider_bill_validation_rows.csv",
        "direct_rider_family_csv": output_dir / "dep_res_direct_rider_family_audit.csv",
    }

    paths["summary_json"].write_text(json.dumps(report["summary"], indent=2), encoding="utf-8")

    for key, frame in [
        ("component_completeness_csv", report["component_completeness"]),
        ("component_date_matrix_csv", report["component_date_matrix"]),
        ("suspect_rows_csv", report["suspect_component_rows"]),
        ("bill_assessment_summary_csv", report["bill_assessment_summary"]),
        ("bill_validation_rows_csv", report["bill_validation_rows"]),
        ("direct_rider_family_csv", report["direct_rider_family_audit"]),
    ]:
        if isinstance(frame, pd.DataFrame):
            frame.to_csv(paths[key], index=False)

    return paths


def _load_direct_rider_family_audit(database_path: Path) -> Any:
    pd = _require_pandas()
    conn = sqlite3.connect(database_path)
    try:
        return pd.read_sql_query(
            """
            SELECT
                title,
                effective_start,
                local_path,
                CASE
                    WHEN lower(title) LIKE '%storm%' OR lower(title) LIKE '%sts%'
                    THEN 'storm_recovery_charge'
                    WHEN lower(title) LIKE '%annual billing%' OR lower(title) LIKE '%leaf-no-601%'
                    THEN 'clean_energy_rider'
                    ELSE 'other'
                END AS rider_family
            FROM historical_documents
            WHERE state = 'NC'
              AND company = 'progress'
              AND (
                    lower(title) LIKE '%storm%'
                 OR lower(title) LIKE '%sts%'
                 OR lower(title) LIKE '%annual billing%'
                 OR lower(title) LIKE '%leaf-no-601%'
              )
            ORDER BY effective_start, title
            """,
            conn,
        )
    finally:
        conn.close()


def _build_summary(
    *,
    component_completeness: Any,
    suspect_component_rows: Any,
    bill_summary: dict[str, Any],
    bill_assessment_summary: Any,
    direct_rider_family_audit: Any,
) -> dict[str, Any]:
    total_rows = int(component_completeness["total_rows"].sum()) if not component_completeness.empty else 0
    dated_rows = int(component_completeness["dated_rows"].sum()) if not component_completeness.empty else 0
    non_ba = component_completeness.loc[component_completeness["rider_code"] != "BA"]
    non_ba_total = int(non_ba["total_rows"].sum()) if not non_ba.empty else 0
    non_ba_dated = int(non_ba["dated_rows"].sum()) if not non_ba.empty else 0

    bill_support_by_component: dict[str, dict[str, int]] = {}
    if not bill_assessment_summary.empty:
        for component_key, group in bill_assessment_summary.groupby("component_key"):
            bill_support_by_component[str(component_key)] = {
                str(row["assessment"]): int(row["row_count"])
                for _, row in group.iterrows()
            }

    direct_family_dates: dict[str, list[str]] = {}
    if not direct_rider_family_audit.empty:
        for rider_family, group in direct_rider_family_audit.groupby("rider_family"):
            dates = sorted(
                {
                    str(value)
                    for value in group["effective_start"].tolist()
                    if value is not None and str(value) != "None"
                }
            )
            direct_family_dates[str(rider_family)] = dates

    return {
        "overall_component_rows": total_rows,
        "overall_component_rows_with_dates": dated_rows,
        "overall_component_date_completeness_pct": round((dated_rows / total_rows * 100.0), 1)
        if total_rows
        else 0.0,
        "non_ba_component_rows": non_ba_total,
        "non_ba_component_rows_with_dates": non_ba_dated,
        "non_ba_component_date_completeness_pct": round((non_ba_dated / non_ba_total * 100.0), 1)
        if non_ba_total
        else 0.0,
        "component_tracking_status": {
            str(row["rider_code"]): str(row["tracking_status"])
            for _, row in component_completeness.iterrows()
        },
        "undated_component_row_count": int(len(suspect_component_rows)),
        "undated_component_rider_codes": sorted(
            {str(value) for value in suspect_component_rows["rider_code"].tolist()}
        )
        if not suspect_component_rows.empty
        else [],
        "bill_rider_date_audit_summary": bill_summary,
        "bill_support_by_component": bill_support_by_component,
        "direct_rider_family_effective_dates": direct_family_dates,
    }


def _tracking_status_for_component(*, rider_code: str, undated_rows: int) -> str:
    if undated_rows == 0:
        return "fully_tracked"
    if rider_code == "BA":
        return "aggregate_rows_undated"
    return "needs_cleanup"


def _tracking_status_for_matrix_row(*, rider_code: str, has_undated_rows: bool) -> str:
    if not has_undated_rows:
        return "dated"
    if rider_code == "BA":
        return "aggregate_rows_undated"
    return "needs_cleanup"


def _csv_count(value: Any) -> int:
    if value is None:
        return 0
    text = str(value)
    if not text or text == "None":
        return 0
    return len([part for part in text.split(",") if part])
