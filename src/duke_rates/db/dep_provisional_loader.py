from __future__ import annotations

import json
import sqlite3
from datetime import datetime, UTC
from pathlib import Path

from duke_rates.analytics.dep_provisional_riders import (
    load_dep_res_provisional_rider_history,
)
from duke_rates.db.sqlite import connect


_NOW = datetime.now(UTC).isoformat()


def _normalize_date_value(value) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def load_dep_res_provisional_history(
    database_path: Path,
    *,
    schedule_code: str = "RES",
    start_date: str = "2016-01-01",
    end_date: str = "2022-12-31",
    replace: bool = True,
) -> dict[str, int]:
    totals_df, components_df = load_dep_res_provisional_rider_history(
        database_path=database_path,
        start_date=start_date,
        end_date=end_date,
    )

    if totals_df.empty:
        return {"totals_loaded": 0, "components_loaded": 0}

    conn = connect(database_path)
    try:
        return _load_frames(
            conn,
            totals_df=totals_df,
            components_df=components_df,
            schedule_code=schedule_code,
            replace=replace,
        )
    finally:
        conn.close()


def _load_frames(
    conn: sqlite3.Connection,
    *,
    totals_df,
    components_df,
    schedule_code: str,
    replace: bool,
) -> dict[str, int]:
    totals_loaded = 0
    components_loaded = 0

    for _, total_row in totals_df.iterrows():
        effective_date = _normalize_date_value(total_row["effective_date"])
        conn.execute(
            """
            DELETE FROM dep_provisional_rider_components
            WHERE schedule_code = ?
              AND effective_date LIKE ?
            """,
            (schedule_code, f"{effective_date} %"),
        )
        conn.execute(
            """
            DELETE FROM dep_provisional_rider_totals
            WHERE schedule_code = ?
              AND effective_date LIKE ?
            """,
            (schedule_code, f"{effective_date} %"),
        )
        existing = conn.execute(
            """
            SELECT id
            FROM dep_provisional_rider_totals
            WHERE schedule_code = ? AND effective_date = ?
            """,
            (schedule_code, effective_date),
        ).fetchone()

        payload = (
            schedule_code,
            effective_date,
            total_row.get("docket_dir"),
            total_row.get("source_pdf"),
            int(total_row.get("component_count", 0) or 0),
            json.dumps(
                [code for code in str(total_row.get("component_codes", "")).split(",") if code]
            ),
            float(total_row["provisional_rider_cents_per_kwh"])
            if total_row.get("provisional_rider_cents_per_kwh") is not None
            else None,
            total_row.get("coverage_status") or "provisional_partial_components",
            _NOW,
        )

        if existing and not replace:
            total_id = existing["id"]
        elif existing and replace:
            total_id = existing["id"]
            conn.execute(
                """
                UPDATE dep_provisional_rider_totals
                SET docket_dir=?, source_pdf=?, component_count=?, component_codes_json=?,
                    provisional_rider_cents_per_kwh=?, coverage_status=?, created_at=?
                WHERE id=?
                """,
                payload[2:] + (total_id,),
            )
            conn.execute(
                "DELETE FROM dep_provisional_rider_components WHERE total_id = ?",
                (total_id,),
            )
            totals_loaded += 1
        else:
            cur = conn.execute(
                """
                INSERT INTO dep_provisional_rider_totals (
                    schedule_code, effective_date, docket_dir, source_pdf, component_count,
                    component_codes_json, provisional_rider_cents_per_kwh, coverage_status, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                payload,
            )
            total_id = cur.lastrowid
            totals_loaded += 1

        matching_components = components_df.loc[
            components_df["effective_date"].apply(_normalize_date_value) == effective_date
        ]
        for _, component_row in matching_components.iterrows():
            conn.execute(
                """
                INSERT OR REPLACE INTO dep_provisional_rider_components (
                    total_id, schedule_code, effective_date, rider_code, rider_effective_date,
                    cents_per_kwh, docket_dir, source_pdf, component_source_pdf,
                    component_source_docket_dir, parser_source, source_pages, created_at
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                """,
                (
                    total_id,
                    schedule_code,
                    effective_date,
                    component_row["rider_code"],
                    component_row["rider_effective_date"].strftime("%Y-%m-%d")
                    if getattr(component_row["rider_effective_date"], "strftime", None)
                    else str(component_row["rider_effective_date"]),
                    float(component_row["cents_per_kwh"])
                    if component_row.get("cents_per_kwh") is not None
                    else None,
                    component_row.get("docket_dir"),
                    component_row.get("source_pdf"),
                    component_row.get("component_source_pdf"),
                    component_row.get("component_source_docket_dir"),
                    component_row.get("parser_source"),
                    component_row.get("source_pages"),
                    _NOW,
                ),
            )
            components_loaded += 1

    conn.commit()
    return {"totals_loaded": totals_loaded, "components_loaded": components_loaded}
