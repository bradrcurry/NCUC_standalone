from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from bisect import bisect_right

from duke_rates.analytics.dep_progress import (
    _connect,
    _require_pandas,
    load_dep_res_base_history,
    load_dep_res_rider_history,
)
from duke_rates.analytics.dep_provisional_riders import load_dep_res_provisional_rider_history

RAL2_RETIREMENT_DATE = "2024-10-01"


def load_dep_res_validation_report(
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
) -> dict[str, Any]:
    pd = _require_pandas()

    base_df = load_dep_res_base_history(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
    )
    rider_totals_df, rider_components_df = load_dep_res_rider_history(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
    )
    provisional_totals_df, provisional_components_df = load_dep_res_provisional_rider_history(
        database_path=database_path,
        start_date=start_date,
        end_date=min(end_date, "2022-12-31"),
    )

    with _connect(database_path) as conn:
        duplicate_base_rows = pd.read_sql_query(
            """
            SELECT effective_date, COUNT(*) AS raw_row_count
            FROM ncuc_ingest_segments
            WHERE status IN ('parsed', 'partial')
              AND effective_date IS NOT NULL
              AND effective_date BETWEEN ? AND ?
              AND (schedule_code = 'RES' OR schedule_code LIKE 'RES-%')
              AND (leaf_no = '500' OR leaf_no IS NULL)
            GROUP BY effective_date
            HAVING COUNT(*) > 1
            ORDER BY effective_date
            """,
            conn,
            params=(start_date, end_date),
        )
        null_date_base_rows = pd.read_sql_query(
            """
            SELECT id, docket_dir, source_pdf, docket_number, status, confidence
            FROM ncuc_ingest_segments
            WHERE status IN ('parsed', 'partial')
              AND effective_date IS NULL
              AND (schedule_code = 'RES' OR schedule_code LIKE 'RES-%')
              AND (leaf_no = '500' OR leaf_no IS NULL)
            ORDER BY id
            """,
            conn,
        )
        null_date_rider_blocks = pd.read_sql_query(
            """
            SELECT id, rate_class, docket_dir, source_pdf, docket_number, total_cents_per_kwh
            FROM rider_summary_blocks
            WHERE rate_class = 'Residential Service Schedules'
              AND effective_date IS NULL
            ORDER BY id
            """,
            conn,
        )

    base_dates = {
        value.strftime("%Y-%m-%d")
        for value in base_df["effective_date"]
    } if not base_df.empty else set()
    clean_rider_dates = {
        value.strftime("%Y-%m-%d")
        for value in rider_totals_df["effective_date"]
    } if not rider_totals_df.empty else set()
    provisional_dates = {
        value
        for value in provisional_totals_df["effective_date"].astype(str)
    } if not provisional_totals_df.empty else set()

    expected_rider_codes = sorted(set(rider_components_df["rider_code"])) if not rider_components_df.empty else []
    applicable_schedule_map = {
        "RES": expected_rider_codes,
        "R-TOUD": expected_rider_codes,
        "R-TOU": expected_rider_codes,
        "R-TOU-CPP": expected_rider_codes,
        "R-TOUE": expected_rider_codes,
        "R-TOU-EV": expected_rider_codes,
    }

    clean_rows: list[dict[str, Any]] = []
    if not rider_components_df.empty:
        for effective_date, group in rider_components_df.groupby("effective_date"):
            date_key = effective_date.strftime("%Y-%m-%d")
            codes = sorted(set(group["rider_code"]))
            expected_codes_for_date = _expected_clean_rider_codes_for_date(date_key, expected_rider_codes)
            missing_codes = [code for code in expected_codes_for_date if code not in codes]
            total_row = rider_totals_df.loc[rider_totals_df["effective_date"] == effective_date].iloc[0]
            clean_rows.append(
                {
                    "effective_date": date_key,
                    "parsed_rider_codes": ",".join(codes),
                    "parsed_rider_count": len(codes),
                    "expected_rider_codes": ",".join(expected_codes_for_date),
                    "expected_rider_count": len(expected_codes_for_date),
                    "missing_rider_codes": ",".join(missing_codes),
                    "missing_rider_count": len(missing_codes),
                    "total_rider_cents_per_kwh": float(total_row["total_rider_cents_per_kwh"]),
                    "quality_flag": total_row["quality_flag"],
                    "source_pdf": total_row["source_pdf"],
                    "docket_dir": total_row["docket_dir"],
                }
            )
    clean_validation_df = pd.DataFrame(clean_rows)

    provisional_rows: list[dict[str, Any]] = []
    if not provisional_components_df.empty:
        for effective_date, group in provisional_components_df.groupby("effective_date"):
            date_key = effective_date.strftime("%Y-%m-%d")
            codes = sorted(set(group["rider_code"]))
            provisional_total_row = provisional_totals_df.loc[
                provisional_totals_df["effective_date"] == date_key
            ].iloc[0]
            provisional_rows.append(
                {
                    "effective_date": date_key,
                    "parsed_rider_codes": ",".join(codes),
                    "parsed_rider_count": len(codes),
                    "source_pdf": provisional_total_row["source_pdf"],
                    "docket_dir": provisional_total_row["docket_dir"],
                    "coverage_status": provisional_total_row["coverage_status"],
                    "provisional_rider_cents_per_kwh": float(
                        provisional_total_row["provisional_rider_cents_per_kwh"]
                    ),
                }
            )
    provisional_validation_df = pd.DataFrame(provisional_rows)

    base_without_any_riders = sorted(base_dates - clean_rider_dates - provisional_dates)
    clean_missing_pre_2023 = sorted(date for date in base_dates if date < "2023-10-01" and date not in provisional_dates)
    clean_missing_post_2023 = sorted(date for date in base_dates if date >= "2023-10-01" and date not in clean_rider_dates)
    partial_clean_rider_dates = sorted(
        row["effective_date"]
        for row in clean_rows
        if row["missing_rider_count"] > 0 or row["quality_flag"] != "ok"
    )
    coverage_rows = _build_base_rider_coverage_rows(
        base_df=base_df,
        rider_totals_df=rider_totals_df,
        provisional_totals_df=provisional_totals_df,
    )
    coverage_df = pd.DataFrame(coverage_rows)
    uncovered_base_dates = sorted(
        row["base_effective_date"]
        for row in coverage_rows
        if row["coverage_status"] == "uncovered"
    )
    exact_date_gap_but_covered = sorted(
        row["base_effective_date"]
        for row in coverage_rows
        if row["coverage_status"] == "carried_forward"
    )

    summary = {
        "base_distinct_effective_dates": len(base_dates),
        "clean_rider_distinct_effective_dates": len(clean_rider_dates),
        "provisional_rider_distinct_effective_dates": len(provisional_dates),
        "expected_clean_rider_codes": expected_rider_codes,
        "retired_clean_rider_codes": {"RAL-2": RAL2_RETIREMENT_DATE},
        "applicable_riders_by_schedule": applicable_schedule_map,
        "base_dates_without_any_rider_series": base_without_any_riders,
        "missing_provisional_pre_2023_base_dates": clean_missing_pre_2023,
        "missing_clean_rider_post_2023_base_dates": clean_missing_post_2023,
        "base_dates_with_no_same_day_rider_but_covered_by_carry_forward": exact_date_gap_but_covered,
        "base_dates_without_any_rider_coverage": uncovered_base_dates,
        "partial_clean_rider_dates": partial_clean_rider_dates,
        "duplicate_base_effective_date_count": int(len(duplicate_base_rows)),
        "null_date_base_row_count": int(len(null_date_base_rows)),
        "null_date_rider_block_count": int(len(null_date_rider_blocks)),
    }

    return {
        "summary": summary,
        "clean_rider_validation": clean_validation_df,
        "provisional_rider_validation": provisional_validation_df,
        "base_rider_coverage": coverage_df,
        "duplicate_base_rows": duplicate_base_rows,
        "null_date_base_rows": null_date_base_rows,
        "null_date_rider_blocks": null_date_rider_blocks,
    }


def _expected_clean_rider_codes_for_date(
    effective_date: str,
    expected_rider_codes: list[str],
) -> list[str]:
    if effective_date >= RAL2_RETIREMENT_DATE:
        return [code for code in expected_rider_codes if code != "RAL-2"]
    return list(expected_rider_codes)


def export_dep_res_validation_report(
    output_dir: Path,
    *,
    database_path: Path | None = None,
    start_date: str = "2016-01-01",
    end_date: str = "2026-12-31",
) -> dict[str, Path]:
    pd = _require_pandas()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = load_dep_res_validation_report(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
    )

    paths = {
        "summary_json": output_dir / "dep_res_validation_summary.json",
        "clean_rider_validation_csv": output_dir / "dep_res_clean_rider_validation.csv",
        "provisional_rider_validation_csv": output_dir / "dep_res_provisional_rider_validation.csv",
        "base_rider_coverage_csv": output_dir / "dep_res_base_rider_coverage.csv",
        "duplicate_base_rows_csv": output_dir / "dep_res_duplicate_base_rows.csv",
        "null_date_base_rows_csv": output_dir / "dep_res_null_date_base_rows.csv",
        "null_date_rider_blocks_csv": output_dir / "dep_res_null_date_rider_blocks.csv",
    }

    paths["summary_json"].write_text(json.dumps(report["summary"], indent=2))

    for key, frame in [
        ("clean_rider_validation_csv", report["clean_rider_validation"]),
        ("provisional_rider_validation_csv", report["provisional_rider_validation"]),
        ("base_rider_coverage_csv", report["base_rider_coverage"]),
        ("duplicate_base_rows_csv", report["duplicate_base_rows"]),
        ("null_date_base_rows_csv", report["null_date_base_rows"]),
        ("null_date_rider_blocks_csv", report["null_date_rider_blocks"]),
    ]:
        if isinstance(frame, pd.DataFrame):
            frame.to_csv(paths[key], index=False)

    return paths


def _build_base_rider_coverage_rows(
    *,
    base_df: Any,
    rider_totals_df: Any,
    provisional_totals_df: Any,
) -> list[dict[str, Any]]:
    coverage_candidates: list[dict[str, Any]] = []
    if not provisional_totals_df.empty:
        for row in provisional_totals_df.to_dict("records"):
            coverage_candidates.append(
                {
                    "effective_date": _normalize_date_key(row["effective_date"]),
                    "coverage_source": "provisional",
                    "rider_cents_per_kwh": float(row["provisional_rider_cents_per_kwh"]),
                    "source_pdf": row["source_pdf"],
                }
            )
    if not rider_totals_df.empty:
        for row in rider_totals_df.to_dict("records"):
            coverage_candidates.append(
                {
                    "effective_date": _normalize_date_key(row["effective_date"]),
                    "coverage_source": "clean",
                    "rider_cents_per_kwh": float(row["total_rider_cents_per_kwh"]),
                    "source_pdf": row["source_pdf"],
                }
            )

    coverage_candidates.sort(key=lambda row: row["effective_date"])
    candidate_dates = [row["effective_date"] for row in coverage_candidates]

    coverage_rows: list[dict[str, Any]] = []
    if base_df.empty:
        return coverage_rows

    for row in base_df.to_dict("records"):
        base_effective_date = row["effective_date"].strftime("%Y-%m-%d")
        pos = bisect_right(candidate_dates, base_effective_date) - 1
        if pos < 0:
            coverage_rows.append(
                {
                    "base_effective_date": base_effective_date,
                    "base_source_pdf": row["source_pdf"],
                    "matched_rider_effective_date": None,
                    "matched_rider_source": None,
                    "matched_rider_cents_per_kwh": None,
                    "matched_rider_pdf": None,
                    "coverage_status": "uncovered",
                }
            )
            continue

        match = coverage_candidates[pos]
        coverage_rows.append(
            {
                "base_effective_date": base_effective_date,
                "base_source_pdf": row["source_pdf"],
                "matched_rider_effective_date": match["effective_date"],
                "matched_rider_source": match["coverage_source"],
                "matched_rider_cents_per_kwh": match["rider_cents_per_kwh"],
                "matched_rider_pdf": match["source_pdf"],
                "coverage_status": (
                    "same_day"
                    if match["effective_date"] == base_effective_date
                    else "carried_forward"
                ),
            }
        )

    return coverage_rows


def _normalize_date_key(value: Any) -> str:
    text = str(value)
    if " " in text:
        text = text.split(" ", 1)[0]
    if "T" in text:
        text = text.split("T", 1)[0]
    return text
