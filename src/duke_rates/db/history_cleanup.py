from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from duke_rates.db.sqlite import connect
from duke_rates.parse.heuristics import extract_effective_date
from duke_rates.parse.pdf_text import extract_pdf_text
from duke_rates.parse.rider_summary import parse_rider_summary


@dataclass
class CleanupDecision:
    keep_id: int
    delete_ids: list[int]
    group_key: str
    rows: list[dict[str, Any]]


def cleanup_nc_residential_history(
    database_path: Path,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    conn = connect(database_path)
    conn.row_factory = sqlite3.Row
    try:
        duplicate_decisions = _build_base_duplicate_decisions(conn)
        null_base_delete_ids = _find_safe_null_base_delete_ids(conn)
        null_rider_block_delete_ids = _find_safe_null_rider_block_delete_ids(conn)
        inferred_base_updates = _infer_null_base_updates(conn)
        inferred_rider_updates = _infer_null_rider_block_updates(conn)

        report = {
            "duplicate_base_groups": len(duplicate_decisions),
            "duplicate_base_rows_to_delete": sum(len(item.delete_ids) for item in duplicate_decisions),
            "null_base_rows_to_delete": len(null_base_delete_ids),
            "null_rider_blocks_to_delete": len(null_rider_block_delete_ids),
            "null_rider_line_items_to_delete": _count_rider_line_items(conn, null_rider_block_delete_ids),
            "inferred_base_updates": inferred_base_updates,
            "inferred_rider_updates": inferred_rider_updates,
            "duplicate_base_decisions": [
                {
                    "group_key": item.group_key,
                    "keep_id": item.keep_id,
                    "delete_ids": item.delete_ids,
                    "rows": item.rows,
                }
                for item in duplicate_decisions
            ],
            "null_base_rows": _fetch_rows_by_ids(
                conn,
                table="ncuc_ingest_segments",
                row_ids=null_base_delete_ids,
                columns=[
                    "id",
                    "schedule_code",
                    "effective_date",
                    "docket_dir",
                    "source_pdf",
                    "leaf_no",
                    "status",
                    "confidence",
                ],
            ),
            "null_rider_blocks": _fetch_rows_by_ids(
                conn,
                table="rider_summary_blocks",
                row_ids=null_rider_block_delete_ids,
                columns=[
                    "id",
                    "effective_date",
                    "docket_dir",
                    "source_pdf",
                    "leaf_no",
                    "rate_class",
                    "docket_number",
                ],
            ),
            "applied": False,
        }

        if apply:
            duplicate_delete_ids = [row_id for item in duplicate_decisions for row_id in item.delete_ids]
            if duplicate_delete_ids:
                _delete_rows(conn, "ncuc_ingest_segments", duplicate_delete_ids)
            for item in inferred_base_updates:
                if item["action"] == "update":
                    conn.execute(
                        "UPDATE ncuc_ingest_segments SET effective_date = ? WHERE id = ?",
                        (item["effective_date"], item["id"]),
                    )
                elif item["action"] == "delete":
                    _delete_rows(conn, "ncuc_ingest_segments", [int(item["id"])])
            if null_base_delete_ids:
                _delete_rows(conn, "ncuc_ingest_segments", null_base_delete_ids)
            for item in inferred_rider_updates:
                if item["action"] == "update":
                    conn.execute(
                        "UPDATE rider_summary_blocks SET effective_date = ? WHERE id = ?",
                        (item["effective_date"], item["id"]),
                    )
                elif item["action"] == "delete":
                    _delete_rows(conn, "rider_line_items", _fetch_rider_line_item_ids(conn, [int(item["id"])]))
                    _delete_rows(conn, "rider_summary_blocks", [int(item["id"])])
            if null_rider_block_delete_ids:
                _delete_rows(conn, "rider_line_items", _fetch_rider_line_item_ids(conn, null_rider_block_delete_ids))
                _delete_rows(conn, "rider_summary_blocks", null_rider_block_delete_ids)
            conn.commit()
            report["applied"] = True

        return report
    finally:
        conn.close()


def export_cleanup_report(report: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2))
    return output_path


def _build_base_duplicate_decisions(conn: sqlite3.Connection) -> list[CleanupDecision]:
    groups = conn.execute(
        """
        SELECT schedule_code, effective_date
        FROM ncuc_ingest_segments
        WHERE schedule_code IN ('RES', 'RS')
          AND effective_date IS NOT NULL
        GROUP BY schedule_code, effective_date
        HAVING COUNT(*) > 1
        ORDER BY schedule_code, effective_date
        """
    ).fetchall()

    decisions: list[CleanupDecision] = []
    for group in groups:
        rows = conn.execute(
            """
            SELECT id, schedule_code, effective_date, docket_dir, source_pdf, leaf_no,
                   status, confidence, revision_label
            FROM ncuc_ingest_segments
            WHERE schedule_code = ? AND effective_date = ?
            ORDER BY id
            """,
            (group["schedule_code"], group["effective_date"]),
        ).fetchall()
        if len(rows) < 2:
            continue
        sorted_rows = sorted(rows, key=_base_row_sort_key, reverse=True)
        keep = sorted_rows[0]
        delete_ids = [int(row["id"]) for row in sorted_rows[1:]]
        decisions.append(
            CleanupDecision(
                keep_id=int(keep["id"]),
                delete_ids=delete_ids,
                group_key=f"{group['schedule_code']}:{group['effective_date']}",
                rows=[dict(row) for row in rows],
            )
        )
    return decisions


def _base_row_sort_key(row: sqlite3.Row) -> tuple[Any, ...]:
    source_pdf = (row["source_pdf"] or "").lower()
    docket_dir = (row["docket_dir"] or "").lower()
    status = (row["status"] or "").lower()
    leaf_no = (row["leaf_no"] or "").lower()

    schedule_code = row["schedule_code"]
    utility_match = 0
    if schedule_code == "RES" and docket_dir.startswith("e-2-"):
        utility_match = 1
    if schedule_code == "RS" and docket_dir.startswith("e-7-"):
        utility_match = 1

    return (
        1 if "local_raw" in docket_dir else 0,
        1 if source_pdf.startswith("data\\raw\\") or source_pdf.startswith("data\\historical\\raw\\") else 0,
        utility_match,
        1 if status == "parsed" else 0,
        float(row["confidence"] or 0.0),
        1 if leaf_no in {"500", "11"} else 0,
        1 if row["revision_label"] else 0,
        int(row["id"]),
    )


def _find_safe_null_base_delete_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        """
        SELECT id, schedule_code, source_pdf, leaf_no, confidence, status
        FROM ncuc_ingest_segments
        WHERE schedule_code IN ('RES', 'RS')
          AND effective_date IS NULL
        ORDER BY id
        """
    ).fetchall()
    delete_ids: list[int] = []
    for row in rows:
        source_pdf = row["source_pdf"] or ""
        leaf_no = row["leaf_no"] or ""
        if leaf_no not in {"500", "11"}:
            delete_ids.append(int(row["id"]))
            continue
        matching_non_null = conn.execute(
            """
            SELECT COUNT(*)
            FROM ncuc_ingest_segments
            WHERE source_pdf = ?
              AND schedule_code = ?
              AND COALESCE(leaf_no, '') = COALESCE(?, '')
              AND effective_date IS NOT NULL
            """,
            (source_pdf, row["schedule_code"], leaf_no),
        ).fetchone()[0]
        if matching_non_null:
            delete_ids.append(int(row["id"]))
    return delete_ids


def _find_safe_null_rider_block_delete_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        """
        SELECT id, source_pdf, rate_class
        FROM rider_summary_blocks
        WHERE effective_date IS NULL
          AND rate_class = 'Residential Service Schedules'
        ORDER BY id
        """
    ).fetchall()
    delete_ids: list[int] = []
    for row in rows:
        matching_non_null = conn.execute(
            """
            SELECT COUNT(*)
            FROM rider_summary_blocks
            WHERE source_pdf = ?
              AND rate_class = ?
              AND effective_date IS NOT NULL
            """,
            (row["source_pdf"], row["rate_class"]),
        ).fetchone()[0]
        if matching_non_null:
            delete_ids.append(int(row["id"]))
    return delete_ids


def _infer_null_base_updates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, schedule_code, source_pdf, leaf_no
        FROM ncuc_ingest_segments
        WHERE schedule_code IN ('RES', 'RS')
          AND effective_date IS NULL
        ORDER BY id
        """
    ).fetchall()
    updates: list[dict[str, Any]] = []
    for row in rows:
        source_path = Path(row["source_pdf"])
        if not source_path.exists():
            continue
        inferred = _normalize_effective_date_value(extract_effective_date(extract_pdf_text(source_path)))
        if not inferred:
            continue
        duplicate_exists = conn.execute(
            """
            SELECT id
            FROM ncuc_ingest_segments
            WHERE schedule_code = ?
              AND effective_date = ?
              AND id <> ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (row["schedule_code"], inferred, row["id"]),
        ).fetchone()
        updates.append(
            {
                "id": int(row["id"]),
                "schedule_code": row["schedule_code"],
                "source_pdf": row["source_pdf"],
                "effective_date": inferred,
                "action": "delete" if duplicate_exists else "update",
                "duplicate_of_id": int(duplicate_exists["id"]) if duplicate_exists else None,
            }
        )
    return updates


def _infer_null_rider_block_updates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, source_pdf, rate_class, leaf_no
        FROM rider_summary_blocks
        WHERE effective_date IS NULL
          AND rate_class = 'Residential Service Schedules'
        ORDER BY id
        """
    ).fetchall()
    updates: list[dict[str, Any]] = []
    for row in rows:
        source_path = Path(row["source_pdf"])
        if not source_path.exists():
            continue
        text = extract_pdf_text(source_path)
        inferred = _normalize_effective_date_value(parse_rider_summary(text, source_pdf=str(source_path), leaf_no=row["leaf_no"]).effective_date)
        if not inferred:
            continue
        duplicate_exists = conn.execute(
            """
            SELECT id
            FROM rider_summary_blocks
            WHERE rate_class = ?
              AND effective_date = ?
              AND id <> ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (row["rate_class"], inferred, row["id"]),
        ).fetchone()
        updates.append(
            {
                "id": int(row["id"]),
                "rate_class": row["rate_class"],
                "source_pdf": row["source_pdf"],
                "effective_date": inferred,
                "action": "delete" if duplicate_exists else "update",
                "duplicate_of_id": int(duplicate_exists["id"]) if duplicate_exists else None,
            }
        )
    return updates


def _fetch_rows_by_ids(
    conn: sqlite3.Connection,
    *,
    table: str,
    row_ids: list[int],
    columns: list[str],
) -> list[dict[str, Any]]:
    if not row_ids:
        return []
    placeholders = ",".join("?" for _ in row_ids)
    sql = f"SELECT {', '.join(columns)} FROM {table} WHERE id IN ({placeholders}) ORDER BY id"
    return [dict(row) for row in conn.execute(sql, row_ids).fetchall()]


def _fetch_rider_line_item_ids(conn: sqlite3.Connection, block_ids: list[int]) -> list[int]:
    if not block_ids:
        return []
    placeholders = ",".join("?" for _ in block_ids)
    sql = f"SELECT id FROM rider_line_items WHERE block_id IN ({placeholders})"
    return [int(row[0]) for row in conn.execute(sql, block_ids).fetchall()]


def _count_rider_line_items(conn: sqlite3.Connection, block_ids: list[int]) -> int:
    if not block_ids:
        return 0
    placeholders = ",".join("?" for _ in block_ids)
    sql = f"SELECT COUNT(*) FROM rider_line_items WHERE block_id IN ({placeholders})"
    return int(conn.execute(sql, block_ids).fetchone()[0])


def _delete_rows(conn: sqlite3.Connection, table: str, row_ids: list[int]) -> None:
    if not row_ids:
        return
    placeholders = ",".join("?" for _ in row_ids)
    conn.execute(f"DELETE FROM {table} WHERE id IN ({placeholders})", row_ids)


def _normalize_effective_date_value(value: str | None) -> str | None:
    if not value:
        return None
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %Y", "%b %Y"):
        try:
            parsed = datetime.strptime(value, fmt)
            if fmt in ("%B %Y", "%b %Y"):
                parsed = parsed.replace(day=1)
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None
