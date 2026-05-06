from __future__ import annotations

import json
import re
from typing import Any

from duke_rates.db.repository import Repository


_INVALID_TEXT_SENTINELS = {"", "none", "null"}
_DOCKET_DIR_RE = re.compile(r"(e-\d+-sub-\d+)", re.IGNORECASE)


def _valid_text(value: Any) -> bool:
    if value is None:
        return False
    normalized = str(value).strip()
    if not normalized:
        return False
    return normalized.lower() not in _INVALID_TEXT_SENTINELS


def _valid_text_sql(expr: str) -> str:
    return (
        f"({expr} IS NOT NULL AND TRIM(CAST({expr} AS TEXT)) <> '' "
        f"AND LOWER(TRIM(CAST({expr} AS TEXT))) NOT IN ('none', 'null'))"
    )


def _infer_docket_dir(local_path: str | None) -> str | None:
    if not _valid_text(local_path):
        return None
    match = _DOCKET_DIR_RE.search(str(local_path).replace("/", "\\"))
    if not match:
        return None
    return match.group(1).lower()


def _lookup_discovery_match(
    conn,
    *,
    local_path: str | None,
    content_hash: str | None,
) -> dict[str, Any]:
    path_row = None
    hash_row = None
    if _valid_text(local_path):
        path_row = conn.execute(
            """
            SELECT id, docket_number, filing_date, fetch_status
            FROM ncuc_discovery_records
            WHERE local_path = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (str(local_path),),
        ).fetchone()
    if _valid_text(content_hash):
        hash_row = conn.execute(
            """
            SELECT id, docket_number, filing_date, fetch_status
            FROM ncuc_discovery_records
            WHERE content_hash = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (str(content_hash),),
        ).fetchone()

    if path_row and hash_row:
        selected = hash_row
        linkage = "path+hash"
    elif hash_row:
        selected = hash_row
        linkage = "hash_only"
    elif path_row:
        selected = path_row
        linkage = "path_only"
    else:
        selected = None
        linkage = "missing"

    return {
        "linkage": linkage,
        "record_id": int(selected["id"]) if selected else None,
        "docket_number": selected["docket_number"] if selected else None,
        "filing_date": selected["filing_date"] if selected else None,
        "fetch_status": selected["fetch_status"] if selected else None,
    }


def _build_candidate_fill_fields(row: dict[str, Any], discovery_match: dict[str, Any]) -> list[str]:
    candidate_fields: list[str] = []
    if row["missing_leaf_no"] and _valid_text(row.get("historical_leaf_no")):
        candidate_fields.append("leaf_no")
    if row["missing_source_pdf"] and _valid_text(row.get("local_path")):
        candidate_fields.append("source_pdf")
    if row["missing_docket_dir"] and _infer_docket_dir(row.get("local_path")):
        candidate_fields.append("docket_dir")
    if row["missing_docket_number"] and _valid_text(discovery_match.get("docket_number")):
        candidate_fields.append("docket_number")
    if row["missing_order_date"] and _valid_text(discovery_match.get("filing_date")):
        candidate_fields.append("order_date")
    return candidate_fields


def _row_missing_fields(row: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    if row["missing_docket_number"]:
        fields.append("docket_number")
    if row["missing_order_date"]:
        fields.append("order_date")
    if row["missing_leaf_no"]:
        fields.append("leaf_no")
    if row["missing_source_pdf"]:
        fields.append("source_pdf")
    if row["missing_docket_dir"]:
        fields.append("docket_dir")
    return fields


def build_provenance_gap_report(
    repo: Repository,
    *,
    limit: int = 25,
) -> dict[str, Any]:
    valid_tv_docket = _valid_text_sql("tv.docket_number")
    valid_tv_order = _valid_text_sql("tv.order_date")
    valid_tv_leaf = _valid_text_sql("tv.leaf_no")
    valid_tv_source_pdf = _valid_text_sql("tv.source_pdf")
    valid_tv_docket_dir = _valid_text_sql("tv.docket_dir")
    valid_hd_local_path = _valid_text_sql("hd.local_path")
    valid_hd_hash = _valid_text_sql("hd.content_hash")
    valid_dr_docket = _valid_text_sql("docket_number")

    version_gap_clause = (
        f"NOT {valid_tv_docket} OR NOT {valid_tv_order} OR NOT {valid_tv_leaf} "
        f"OR NOT {valid_tv_source_pdf} OR NOT {valid_tv_docket_dir}"
    )
    discovery_path_match_clause = (
        "EXISTS (SELECT 1 FROM ncuc_discovery_records dr WHERE dr.local_path = hd.local_path)"
    )
    discovery_hash_match_clause = (
        f"{valid_hd_hash} AND EXISTS (SELECT 1 FROM ncuc_discovery_records dr WHERE dr.content_hash = hd.content_hash)"
    )

    with repo._connect() as conn:
        summary = {
            "historical_versions_count": int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM tariff_versions tv
                    JOIN tariff_families tf
                      ON tf.family_key = tv.family_key
                    WHERE tf.state = 'NC'
                      AND tv.historical_document_id IS NOT NULL
                    """
                ).fetchone()[0]
            ),
            "versions_missing_any_provenance_count": int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM tariff_versions tv
                    JOIN tariff_families tf
                      ON tf.family_key = tv.family_key
                    WHERE tf.state = 'NC'
                      AND tv.historical_document_id IS NOT NULL
                      AND ({version_gap_clause})
                    """
                ).fetchone()[0]
            ),
            "versions_missing_docket_number_count": int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM tariff_versions tv
                    JOIN tariff_families tf
                      ON tf.family_key = tv.family_key
                    WHERE tf.state = 'NC'
                      AND tv.historical_document_id IS NOT NULL
                      AND NOT {valid_tv_docket}
                    """
                ).fetchone()[0]
            ),
            "versions_missing_order_date_count": int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM tariff_versions tv
                    JOIN tariff_families tf
                      ON tf.family_key = tv.family_key
                    WHERE tf.state = 'NC'
                      AND tv.historical_document_id IS NOT NULL
                      AND NOT {valid_tv_order}
                    """
                ).fetchone()[0]
            ),
            "versions_missing_leaf_no_count": int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM tariff_versions tv
                    JOIN tariff_families tf
                      ON tf.family_key = tv.family_key
                    WHERE tf.state = 'NC'
                      AND tv.historical_document_id IS NOT NULL
                      AND NOT {valid_tv_leaf}
                    """
                ).fetchone()[0]
            ),
            "versions_missing_source_pdf_count": int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM tariff_versions tv
                    JOIN tariff_families tf
                      ON tf.family_key = tv.family_key
                    WHERE tf.state = 'NC'
                      AND tv.historical_document_id IS NOT NULL
                      AND NOT {valid_tv_source_pdf}
                    """
                ).fetchone()[0]
            ),
            "versions_missing_docket_dir_count": int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM tariff_versions tv
                    JOIN tariff_families tf
                      ON tf.family_key = tv.family_key
                    WHERE tf.state = 'NC'
                      AND tv.historical_document_id IS NOT NULL
                      AND NOT {valid_tv_docket_dir}
                    """
                ).fetchone()[0]
            ),
            "historical_documents_missing_discovery_match_count": int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM historical_documents hd
                    WHERE hd.state = 'NC'
                      AND {valid_hd_local_path}
                      AND NOT ({discovery_path_match_clause})
                      AND NOT ({discovery_hash_match_clause})
                    """
                ).fetchone()[0]
            ),
            "historical_documents_path_only_discovery_link_count": int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM historical_documents hd
                    WHERE hd.state = 'NC'
                      AND {valid_hd_local_path}
                      AND ({discovery_path_match_clause})
                      AND NOT ({discovery_hash_match_clause})
                    """
                ).fetchone()[0]
            ),
            "historical_documents_hash_only_discovery_link_count": int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM historical_documents hd
                    WHERE hd.state = 'NC'
                      AND {valid_hd_hash}
                      AND NOT ({discovery_path_match_clause})
                      AND ({discovery_hash_match_clause})
                    """
                ).fetchone()[0]
            ),
            "acquired_discovery_records_missing_docket_number_count": int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM ncuc_discovery_records
                    WHERE fetch_status IN ('success', 'downloaded')
                      AND NOT {valid_dr_docket}
                    """
                ).fetchone()[0]
            ),
        }

        version_rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT
                    tv.id,
                    tv.family_key,
                    tf.company,
                    tv.historical_document_id,
                    tv.effective_start,
                    tv.source_type,
                    hd.title,
                    hd.leaf_no AS historical_leaf_no,
                    hd.local_path,
                    hd.content_hash,
                    CASE WHEN NOT {valid_tv_docket} THEN 1 ELSE 0 END AS missing_docket_number,
                    CASE WHEN NOT {valid_tv_order} THEN 1 ELSE 0 END AS missing_order_date,
                    CASE WHEN NOT {valid_tv_leaf} THEN 1 ELSE 0 END AS missing_leaf_no,
                    CASE WHEN NOT {valid_tv_source_pdf} THEN 1 ELSE 0 END AS missing_source_pdf,
                    CASE WHEN NOT {valid_tv_docket_dir} THEN 1 ELSE 0 END AS missing_docket_dir
                FROM tariff_versions tv
                JOIN tariff_families tf
                  ON tf.family_key = tv.family_key
                LEFT JOIN historical_documents hd
                  ON hd.id = tv.historical_document_id
                WHERE tf.state = 'NC'
                  AND tv.historical_document_id IS NOT NULL
                  AND ({version_gap_clause})
                ORDER BY
                    (
                        CASE WHEN NOT {valid_tv_docket} THEN 1 ELSE 0 END +
                        CASE WHEN NOT {valid_tv_order} THEN 1 ELSE 0 END +
                        CASE WHEN NOT {valid_tv_leaf} THEN 1 ELSE 0 END +
                        CASE WHEN NOT {valid_tv_source_pdf} THEN 1 ELSE 0 END +
                        CASE WHEN NOT {valid_tv_docket_dir} THEN 1 ELSE 0 END
                    ) DESC,
                    tv.effective_start IS NULL DESC,
                    tv.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]
        versions_missing_provenance: list[dict[str, Any]] = []
        for row in version_rows:
            discovery_match = _lookup_discovery_match(
                conn,
                local_path=row.get("local_path"),
                content_hash=row.get("content_hash"),
            )
            versions_missing_provenance.append(
                {
                    "id": row["id"],
                    "family_key": row["family_key"],
                    "company": row["company"],
                    "historical_document_id": row["historical_document_id"],
                    "effective_start": row["effective_start"],
                    "source_type": row["source_type"],
                    "title": row["title"],
                    "local_path": row["local_path"],
                    "missing_fields": _row_missing_fields(row),
                    "discovery_linkage": discovery_match["linkage"],
                    "matched_discovery_record_id": discovery_match["record_id"],
                    "matched_discovery_docket_number": discovery_match["docket_number"],
                    "matched_discovery_filing_date": discovery_match["filing_date"],
                    "candidate_fill_fields": _build_candidate_fill_fields(row, discovery_match),
                }
            )

        historical_missing_discovery_match = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT id, family_key, company, title, effective_start, leaf_no, local_path
                FROM historical_documents hd
                WHERE hd.state = 'NC'
                  AND {valid_hd_local_path}
                  AND NOT ({discovery_path_match_clause})
                  AND NOT ({discovery_hash_match_clause})
                ORDER BY hd.effective_start IS NULL DESC, hd.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]

        weak_rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT id, family_key, company, title, effective_start, leaf_no, local_path, content_hash
                FROM historical_documents hd
                WHERE hd.state = 'NC'
                  AND {valid_hd_local_path}
                  AND ({discovery_path_match_clause})
                  AND NOT ({discovery_hash_match_clause})
                ORDER BY hd.effective_start IS NULL DESC, hd.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]
        historical_path_only_discovery_link: list[dict[str, Any]] = []
        for row in weak_rows:
            discovery_match = _lookup_discovery_match(
                conn,
                local_path=row.get("local_path"),
                content_hash=row.get("content_hash"),
            )
            historical_path_only_discovery_link.append(
                {
                    "id": row["id"],
                    "family_key": row["family_key"],
                    "company": row["company"],
                    "title": row["title"],
                    "effective_start": row["effective_start"],
                    "leaf_no": row["leaf_no"],
                    "local_path": row["local_path"],
                    "content_hash": row["content_hash"],
                    "matched_discovery_record_id": discovery_match["record_id"],
                    "matched_discovery_docket_number": discovery_match["docket_number"],
                    "matched_discovery_filing_date": discovery_match["filing_date"],
                }
            )

        acquired_discovery_missing_docket_number = []
        for row in conn.execute(
            f"""
            SELECT
                id,
                utility,
                filing_title,
                filing_date,
                fetch_status,
                local_path,
                provenance_notes_json
            FROM ncuc_discovery_records
            WHERE fetch_status IN ('success', 'downloaded')
              AND NOT {valid_dr_docket}
            ORDER BY filing_date DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall():
            notes = []
            try:
                raw_notes = row["provenance_notes_json"]
                if _valid_text(raw_notes):
                    loaded = json.loads(raw_notes)
                    if isinstance(loaded, dict):
                        source = loaded.get("source")
                        label = loaded.get("label")
                        if _valid_text(source):
                            notes.append(f"source={source}")
                        if _valid_text(label):
                            notes.append(f"label={label}")
                    elif isinstance(loaded, list):
                        notes = [str(item) for item in loaded if _valid_text(item)]
            except json.JSONDecodeError:
                notes = []
            acquired_discovery_missing_docket_number.append(
                {
                    "id": row["id"],
                    "utility": row["utility"],
                    "filing_title": row["filing_title"],
                    "filing_date": row["filing_date"],
                    "fetch_status": row["fetch_status"],
                    "local_path": row["local_path"],
                    "provenance_notes": notes,
                }
            )

    return {
        "summary": summary,
        "versions_missing_provenance": versions_missing_provenance,
        "historical_documents_missing_discovery_match": historical_missing_discovery_match,
        "historical_documents_path_only_discovery_link": historical_path_only_discovery_link,
        "acquired_discovery_records_missing_docket_number": acquired_discovery_missing_docket_number,
    }
