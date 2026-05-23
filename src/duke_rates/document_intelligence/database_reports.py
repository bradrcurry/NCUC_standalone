"""Deterministic SQL-based corpus intelligence reports.

Each function accepts a ``sqlite3.Connection`` and returns structured
``dict[str, Any]`` data — never formatted strings.  The master builder
``build_database_intelligence_report`` runs all sub-reports and merges
them into one payload for the CLI, LLM summarization, and overnight loop.

Read-only.  Additive.  No mutation of existing data.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_int(val: Any) -> int | None:
    """Coerce a value to int or return None."""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _query_rows(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple = (),
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Execute a parameterised SELECT and return list-of-dicts."""
    cursor = conn.execute(sql, params)
    rows = [dict(r) for r in cursor.fetchmany(limit)]
    cursor.close()
    return rows


def _count_rows(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple = (),
) -> int:
    """Return the unbounded row count for the given SELECT.

    Wraps the inner query as ``SELECT COUNT(*) FROM (<sql>)`` so the
    backlog size is reported regardless of any LIMIT applied to the
    preview rows. Returns 0 on error so reports never crash a loop.
    """
    try:
        cursor = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params)
        row = cursor.fetchone()
        cursor.close()
        if row is None:
            return 0
        return int(row[0]) if row[0] is not None else 0
    except sqlite3.Error:
        return 0


def _apply_since_filter(
    sql: str,
    since: str | None,
    alias: str = "created_at",
) -> tuple[str, tuple]:
    """Append an optional ``AND alias >= ?`` clause and return (sql, params)."""
    if since:
        return f"{sql} AND {alias} >= ?", (since,)
    return sql, ()


# ---------------------------------------------------------------------------
# 1. find_missing_versions
# ---------------------------------------------------------------------------

def find_missing_versions(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    family_key: str | None = None,
) -> dict[str, Any]:
    """Detect year gaps in tariff/rider version timelines per family.

    Returns families whose observed distinct years are fewer than the
    expected span suggests, implying missing annual versions.
    """
    base_sql = """
        WITH family_year_spans AS (
            SELECT
                tv.family_key,
                COALESCE(tf.company, '') AS company,
                CAST(MIN(CAST(SUBSTR(tv.effective_start, 1, 4) AS INTEGER)) AS INTEGER) AS first_year,
                CAST(MAX(CAST(SUBSTR(tv.effective_start, 1, 4) AS INTEGER)) AS INTEGER) AS last_year,
                COUNT(DISTINCT CAST(SUBSTR(tv.effective_start, 1, 4) AS INTEGER)) AS distinct_years,
                COUNT(DISTINCT tv.id) AS version_count
            FROM tariff_versions tv
            JOIN tariff_families tf ON tf.family_key = tv.family_key
            WHERE tf.state = 'NC'
              AND tv.effective_start IS NOT NULL
              AND tv.effective_start != ''
              AND LENGTH(TRIM(tv.effective_start)) >= 4
            GROUP BY tv.family_key
        )
        SELECT
            family_key,
            company,
            first_year,
            last_year,
            distinct_years,
            version_count,
            (last_year - first_year + 1) AS expected_years,
            (last_year - first_year + 1) - distinct_years AS missing_year_count
        FROM family_year_spans
        WHERE (last_year - first_year + 1) > distinct_years
          AND version_count >= 2
    """

    params: tuple = ()
    if family_key:
        base_sql += " AND family_key = ?"
        params = (family_key,)

    total_count = _count_rows(conn, base_sql, params)
    base_sql += " ORDER BY missing_year_count DESC"

    rows = _query_rows(conn, base_sql, params, limit=limit)

    # Compute the actual missing years per family
    family_rows = []
    for r in rows:
        # Get the distinct observed years
        yr_sql = """
            SELECT DISTINCT CAST(SUBSTR(effective_start, 1, 4) AS INTEGER) AS yr
            FROM tariff_versions
            WHERE family_key = ?
              AND effective_start IS NOT NULL
              AND effective_start != ''
              AND LENGTH(TRIM(effective_start)) >= 4
            ORDER BY yr
        """
        yr_cursor = conn.execute(yr_sql, (r["family_key"],))
        observed_years = [int(row["yr"]) for row in yr_cursor.fetchall()]
        yr_cursor.close()

        first = _safe_int(r.get("first_year"))
        last = _safe_int(r.get("last_year"))
        missing_years: list[int] = []
        if first and last:
            for y in range(first, last + 1):
                if y not in observed_years:
                    missing_years.append(y)

        family_rows.append({
            "family_key": r["family_key"],
            "company": r.get("company", ""),
            "first_year": first,
            "last_year": last,
            "distinct_years": _safe_int(r.get("distinct_years", 0)),
            "version_count": _safe_int(r.get("version_count", 0)),
            "expected_years": _safe_int(r.get("expected_years", 0)),
            "missing_year_count": _safe_int(r.get("missing_year_count", 0)),
            "missing_years": missing_years,
            "observed_years": observed_years,
        })

    return {
        "summary": {
            "count": len(family_rows),
            "total_count": total_count,
            "total_missing_years": sum(r["missing_year_count"] or 0 for r in family_rows),
            "worst_family": family_rows[0]["family_key"] if family_rows else None,
        },
        "rows": family_rows,
    }


# ---------------------------------------------------------------------------
# 2. find_unknown_documents
# ---------------------------------------------------------------------------

def find_unknown_documents(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    docket: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Find documents classified as UNKNOWN, grouped by fingerprint cluster."""
    sql = """
        SELECT
            dc.subject_kind,
            dc.subject_id,
            dc.label,
            dc.confidence,
            dc.classifier,
            dc.classifier_version,
            COALESCE(dfv.cluster_signature_v1, 'no_cluster') AS cluster_signature,
            COUNT(*) OVER (PARTITION BY COALESCE(dfv.cluster_signature_v1, 'no_cluster')) AS cluster_size,
            COALESCE(hd.title, '') AS title,
            hd.local_path AS source_pdf,
            hd.family_key,
            hd.effective_start
        FROM document_classifications dc
        LEFT JOIN historical_documents hd
            ON dc.subject_kind = 'historical_document'
            AND CAST(dc.subject_id AS INTEGER) = hd.id
        LEFT JOIN document_fingerprints_v2 dfv
            ON dfv.source_pdf = hd.local_path
        WHERE dc.label = 'UNKNOWN'
          AND dc.superseded_by IS NULL
    """

    params: tuple = ()
    if docket:
        sql += " AND hd.family_key IN (SELECT family_key FROM tariff_versions WHERE docket_number LIKE ?)"
        params = (f"%{docket}%",)
    if since:
        sql += " AND dc.created_at >= ?"
        params = params + (since,)

    total_count = _count_rows(conn, sql, params)
    sql += " ORDER BY cluster_size DESC, dc.subject_id"

    rows = _query_rows(conn, sql, params, limit=limit)

    # Aggregate by cluster
    clusters: dict[str, dict] = {}
    for r in rows:
        cs = r["cluster_signature"]
        if cs not in clusters:
            clusters[cs] = {
                "cluster_signature": cs,
                "cluster_size": _safe_int(r.get("cluster_size", 0)) or 0,
                "documents": [],
            }
        clusters[cs]["documents"].append({
            "subject_kind": r["subject_kind"],
            "subject_id": r["subject_id"],
            "title": r.get("title", ""),
            "source_pdf": r.get("source_pdf", ""),
            "family_key": r.get("family_key", ""),
            "effective_start": r.get("effective_start", ""),
            "classifier": r.get("classifier", ""),
            "confidence": r.get("confidence", 0.0),
        })

    cluster_list = sorted(clusters.values(), key=lambda c: c["cluster_size"], reverse=True)

    return {
        "summary": {
            "count": len(rows),
            "total_count": total_count,
            "unique_clusters": len(cluster_list),
            "largest_cluster_size": cluster_list[0]["cluster_size"] if cluster_list else 0,
        },
        "rows": cluster_list[:limit],
    }


# ---------------------------------------------------------------------------
# 3. find_low_quality_parses
# ---------------------------------------------------------------------------

def find_low_quality_parses(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    family_key: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Find weak/empty parse results with low or zero charge counts.

    Only the **latest** parse attempt per ``source_pdf`` is considered.
    ``parse_attempt_logs`` is append-only, so older failures persist
    even after the document is later re-extracted successfully.
    Counting every row would treat resolved problems as outstanding.
    """
    # Inner CTE selects the most recent parse attempt per source_pdf,
    # then we filter that single row per document for low-quality
    # signals. This makes the count reflect *currently* failing parses,
    # not the historical accumulation of every failure ever logged.
    sql = """
        WITH latest_attempt AS (
            SELECT pal.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY pal.source_pdf
                       ORDER BY pal.created_at DESC, pal.id DESC
                   ) AS rn
            FROM parse_attempt_logs pal
        )
        SELECT
            pal.id AS parse_attempt_id,
            pal.source_pdf,
            pal.parser_profile,
            pal.status,
            pal.confidence,
            pal.charge_count,
            pal.created_at,
            COALESCE(hd.family_key, '') AS family_key,
            COALESCE(hd.title, '') AS title,
            COALESCE(hd.effective_start, '') AS effective_start
        FROM latest_attempt pal
        LEFT JOIN historical_documents hd
            ON hd.local_path = pal.source_pdf
        WHERE pal.rn = 1
          AND (pal.charge_count = 0
               OR pal.confidence < 0.3
               OR pal.status IN ('empty', 'error'))
    """

    params: tuple = ()
    if family_key:
        sql += " AND hd.family_key = ?"
        params = (family_key,)
    if since:
        sql += " AND pal.created_at >= ?"
        params = params + (since,)
    else:
        sql += " AND pal.created_at >= DATE('now', '-180 days')"

    total_count = _count_rows(conn, sql, params)
    sql += " ORDER BY pal.charge_count ASC, pal.confidence ASC"

    rows = _query_rows(conn, sql, params, limit=limit)

    # Summary by parser_profile and status
    profile_summary: dict[str, int] = {}
    status_summary: dict[str, int] = {}
    for r in rows:
        pp = r.get("parser_profile") or "unknown"
        st = r.get("status") or "unknown"
        profile_summary[pp] = profile_summary.get(pp, 0) + 1
        status_summary[st] = status_summary.get(st, 0) + 1

    return {
        "summary": {
            "count": len(rows),
            "total_count": total_count,
            "zero_charge_count": sum(1 for r in rows if (r.get("charge_count") or 0) == 0),
            "by_parser_profile": dict(
                sorted(profile_summary.items(), key=lambda x: x[1], reverse=True)[:10]
            ),
            "by_status": status_summary,
        },
        "rows": [
            {
                "parse_attempt_id": r["parse_attempt_id"],
                "source_pdf": r.get("source_pdf", ""),
                "parser_profile": r.get("parser_profile", ""),
                "status": r.get("status", ""),
                "confidence": r.get("confidence", 0.0),
                "charge_count": r.get("charge_count", 0),
                "family_key": r.get("family_key", ""),
                "title": r.get("title", ""),
                "effective_start": r.get("effective_start", ""),
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# 4. find_stale_artifacts
# ---------------------------------------------------------------------------

def find_stale_artifacts(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    family_key: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Find documents with missing page/span artifacts and stuck reprocess items."""
    # Pending reprocess queue items
    reprocess_sql = """
        SELECT
            hrq.id,
            hrq.historical_document_id,
            hrq.source_pdf,
            hrq.family_key,
            hrq.queue_reason,
            hrq.priority,
            hrq.status,
            hrq.requested_at,
            'reprocess_queue_pending' AS stale_reason
        FROM historical_reprocess_queue hrq
        WHERE hrq.status = 'pending'
    """
    r_params: tuple = ()
    if family_key:
        reprocess_sql += " AND hrq.family_key = ?"
        r_params = (family_key,)
    if since:
        reprocess_sql += " AND hrq.requested_at >= ?"
        r_params = r_params + (since,)
    reprocess_total = _count_rows(conn, reprocess_sql, r_params)
    reprocess_sql += " ORDER BY hrq.priority DESC, hrq.requested_at"

    # Historical docs missing evidence
    evidence_sql = """
        SELECT
            hd.id,
            hd.id AS historical_document_id,
            hd.local_path AS source_pdf,
            hd.family_key,
            'no_evidence' AS queue_reason,
            25 AS priority,
            'needs_review' AS status,
            hd.retrieved_at AS requested_at,
            'no_evidence_json' AS stale_reason
        FROM historical_documents hd
        WHERE hd.state = 'NC'
          AND hd.local_path IS NOT NULL
          AND (hd.evidence_json IS NULL OR hd.evidence_json = '{}' OR hd.evidence_json = '')
    """
    ev_params: tuple = ()
    if family_key:
        evidence_sql += " AND hd.family_key = ?"
        ev_params = (family_key,)
    if since:
        evidence_sql += " AND hd.retrieved_at >= ?"
        ev_params = ev_params + (since,)
    evidence_total = _count_rows(conn, evidence_sql, ev_params)
    evidence_sql += " ORDER BY hd.id DESC"

    r_rows = _query_rows(conn, reprocess_sql, r_params, limit=limit)
    e_rows = _query_rows(conn, evidence_sql, ev_params, limit=limit)

    all_rows = r_rows + e_rows
    all_rows.sort(key=lambda x: x.get("priority", 0) or 0, reverse=True)
    all_rows = all_rows[:limit]

    by_reason: dict[str, int] = {}
    for r in all_rows:
        reason = r.get("stale_reason") or r.get("queue_reason") or "unknown"
        by_reason[reason] = by_reason.get(reason, 0) + 1

    return {
        "summary": {
            "count": len(all_rows),
            "total_count": reprocess_total + evidence_total,
            "reprocess_queue_pending": len(r_rows),
            "reprocess_queue_pending_total": reprocess_total,
            "no_evidence_json": len(e_rows),
            "no_evidence_json_total": evidence_total,
            "by_reason": by_reason,
        },
        "rows": [
            {
                "historical_document_id": _safe_int(r.get("historical_document_id")),
                "source_pdf": r.get("source_pdf", ""),
                "family_key": r.get("family_key", ""),
                "queue_reason": r.get("queue_reason", ""),
                "stale_reason": r.get("stale_reason", ""),
                "priority": r.get("priority", 0),
                "requested_at": r.get("requested_at", ""),
            }
            for r in all_rows
        ],
    }


# ---------------------------------------------------------------------------
# 5. find_duplicate_documents
# ---------------------------------------------------------------------------

def find_duplicate_documents(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    since: str | None = None,
) -> dict[str, Any]:
    """Detect actually-duplicate historical documents.

    Counts groups of ``historical_documents`` rows that share a
    ``content_hash`` -- the SAME field that ``lineage deduplicate-documents-nc``
    operates on. The previous implementation queried
    ``document_fingerprints_v2.file_hash``, which is a many-to-one
    fingerprint index (one PDF can have multiple fingerprint rows),
    so it reported ~3,400 "duplicate groups" while dedup only had
    ~25 actual groups to drain. The autonomous loop ran dedup 21
    times in a row against an already-empty backlog because of this
    mismatch.
    """
    sql = """
        SELECT
            hd.content_hash,
            COUNT(*) AS duplicate_count,
            GROUP_CONCAT(hd.local_path, '|||') AS source_pdfs_csv,
            GROUP_CONCAT(hd.id) AS id_list_csv
        FROM historical_documents hd
        WHERE hd.content_hash IS NOT NULL
          AND hd.content_hash != ''
          AND hd.state = 'NC'
    """

    params: tuple = ()
    if since:
        sql += " AND hd.retrieved_at >= ?"
        params = (since,)

    sql += """
        GROUP BY hd.content_hash
        HAVING COUNT(*) > 1
    """
    total_count = _count_rows(conn, sql, params)
    sql += " ORDER BY duplicate_count DESC"

    rows = _query_rows(conn, sql, params, limit=limit)

    parsed_rows = []
    total_dup_instances = 0
    for r in rows:
        pdfs = r.get("source_pdfs_csv", "").split("|||") if r.get("source_pdfs_csv") else []
        ids = r.get("id_list_csv", "").split(",") if r.get("id_list_csv") else []
        dup_count = _safe_int(r.get("duplicate_count", 0)) or 0
        total_dup_instances += dup_count
        parsed_rows.append({
            "content_hash": r["content_hash"],
            "duplicate_count": dup_count,
            "source_pdfs": pdfs[:20],  # cap list length
            "historical_document_ids": [int(i) for i in ids if i.strip().isdigit()][:20],
        })

    return {
        "summary": {
            "count": len(parsed_rows),
            "total_count": total_count,
            "total_duplicate_instances": total_dup_instances,
            "max_duplicate_count": parsed_rows[0]["duplicate_count"] if parsed_rows else 0,
        },
        "rows": parsed_rows,
    }


# ---------------------------------------------------------------------------
# 6. find_family_lineage_gaps
# ---------------------------------------------------------------------------

def find_family_lineage_gaps(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    family_key: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Find broken or inconsistent family → version → document chains."""
    gap_type_counts: dict[str, int] = {}

    # Gap type 1: docs with effective_start but no version link
    sql1 = """
        SELECT
            hd.id AS historical_document_id,
            hd.family_key,
            hd.company,
            hd.effective_start,
            hd.title,
            hd.local_path AS source_pdf,
            'no_version_link' AS gap_type
        FROM historical_documents hd
        WHERE hd.state = 'NC'
          AND hd.local_path IS NOT NULL
          AND hd.effective_start IS NOT NULL
          AND hd.effective_start != ''
          AND NOT EXISTS (
              SELECT 1 FROM tariff_versions tv
              WHERE tv.historical_document_id = hd.id
          )
    """
    params1: tuple = ()
    if family_key:
        sql1 += " AND hd.family_key = ?"
        params1 = (family_key,)
    if since:
        sql1 += " AND hd.retrieved_at >= ?"
        params1 = params1 + (since,)
    gap1_total = _count_rows(conn, sql1, params1)
    sql1 += " ORDER BY hd.id DESC"

    rows1 = _query_rows(conn, sql1, params1, limit=limit)
    gap_type_counts["no_version_link"] = gap1_total

    # Gap type 2: docs with null effective_start
    sql2 = """
        SELECT
            hd.id AS historical_document_id,
            hd.family_key,
            hd.company,
            hd.effective_start,
            hd.title,
            hd.local_path AS source_pdf,
            'no_effective_start' AS gap_type
        FROM historical_documents hd
        WHERE hd.state = 'NC'
          AND hd.local_path IS NOT NULL
          AND (hd.effective_start IS NULL OR hd.effective_start = '')
    """
    params2: tuple = ()
    if family_key:
        sql2 += " AND hd.family_key = ?"
        params2 = (family_key,)
    if since:
        sql2 += " AND hd.retrieved_at >= ?"
        params2 = params2 + (since,)
    gap2_total = _count_rows(conn, sql2, params2)
    sql2 += " ORDER BY hd.id DESC"

    rows2 = _query_rows(conn, sql2, params2, limit=limit)
    gap_type_counts["no_effective_start"] = gap2_total

    # Gap type 3: versions with no historical_document_id
    sql3 = """
        SELECT
            tv.id AS version_id,
            tv.family_key,
            COALESCE(tf.company, '') AS company,
            tv.effective_start,
            COALESCE(tv.notes, '') AS title,
            '' AS source_pdf,
            'version_no_doc' AS gap_type
        FROM tariff_versions tv
        JOIN tariff_families tf ON tf.family_key = tv.family_key
        WHERE tf.state = 'NC'
          AND tv.historical_document_id IS NULL
    """
    params3: tuple = ()
    if family_key:
        sql3 += " AND tv.family_key = ?"
        params3 = (family_key,)
    if since:
        sql3 += " AND tv.created_at >= ?"
        params3 = params3 + (since,)
    gap3_total = _count_rows(conn, sql3, params3)
    sql3 += " ORDER BY tv.id DESC"

    rows3 = _query_rows(conn, sql3, params3, limit=limit)
    gap_type_counts["version_no_doc"] = gap3_total

    # Combine
    all_rows_raw = (
        [dict(r) for r in rows1]
        + [dict(r) for r in rows2]
        + [dict(r) for r in rows3]
    )
    all_rows_raw.sort(key=lambda x: x.get("historical_document_id") or x.get("version_id") or 0, reverse=True)
    all_rows_raw = all_rows_raw[:limit]

    return {
        "summary": {
            "count": len(all_rows_raw),
            "total_count": gap1_total + gap2_total + gap3_total,
            "by_gap_type": gap_type_counts,
            "total_no_version_link": gap_type_counts.get("no_version_link", 0),
            "total_no_effective_start": gap_type_counts.get("no_effective_start", 0),
            "total_version_no_doc": gap_type_counts.get("version_no_doc", 0),
        },
        "rows": [
            {
                "historical_document_id": _safe_int(r.get("historical_document_id")),
                "version_id": _safe_int(r.get("version_id")),
                "family_key": r.get("family_key", ""),
                "gap_type": r.get("gap_type", ""),
                "title": r.get("title", ""),
                "effective_start": r.get("effective_start", ""),
                "source_pdf": r.get("source_pdf", ""),
            }
            for r in all_rows_raw
        ],
    }


# ---------------------------------------------------------------------------
# 7. find_docket_coverage_summary
# ---------------------------------------------------------------------------

def find_docket_coverage_summary(
    conn: sqlite3.Connection,
    *,
    limit: int = 200,
    docket: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Summarize document counts per docket, year, and document-type classification."""
    sql = """
        SELECT
            COALESCE(ndr.docket_number, 'unknown') AS docket_number,
            COALESCE(ndr.utility, hd.company, 'unknown') AS utility,
            CAST(
                SUBSTR(
                    COALESCE(NULLIF(hd.effective_start, ''), hd.snapshot_timestamp, '0000'),
                    1, 4
                ) AS INTEGER
            ) AS year,
            COUNT(DISTINCT hd.id) AS doc_count,
            SUM(CASE WHEN dc.label = 'TARIFF_SHEET' THEN 1 ELSE 0 END) AS tariff_sheet_count,
            SUM(CASE WHEN dc.label = 'RIDER' THEN 1 ELSE 0 END) AS rider_count,
            SUM(CASE WHEN dc.label = 'ORDER_FINAL' THEN 1 ELSE 0 END) AS order_count,
            SUM(CASE WHEN dc.label = 'COVER_LETTER' THEN 1 ELSE 0 END) AS cover_letter_count,
            SUM(CASE WHEN dc.label = 'UNKNOWN' THEN 1 ELSE 0 END) AS unknown_count
        FROM historical_documents hd
        LEFT JOIN ncuc_discovery_records ndr
            ON ndr.content_hash = hd.content_hash
        LEFT JOIN document_classifications dc
            ON dc.subject_kind = 'historical_document'
            AND CAST(dc.subject_id AS INTEGER) = hd.id
            AND dc.stage = 'document_type'
            AND dc.superseded_by IS NULL
        WHERE hd.state = 'NC'
    """

    params: tuple = ()
    if docket:
        sql += " AND (ndr.docket_number LIKE ? OR ndr.docket_number = ?)"
        params = (f"%{docket}%", docket)
    if since:
        sql += " AND hd.retrieved_at >= ?"
        params = params + (since,)

    sql += " GROUP BY docket_number, utility, year"
    total_count = _count_rows(conn, sql, params)
    sql += " ORDER BY year DESC, doc_count DESC"

    rows = _query_rows(conn, sql, params, limit=limit)

    # Summary aggregates
    dockets: set[str] = set()
    years: set[int] = set()
    utilities: set[str] = set()
    total_docs = 0
    total_tariff = 0

    for r in rows:
        dockets.add(r.get("docket_number", "unknown"))
        yr = _safe_int(r.get("year"))
        if yr:
            years.add(yr)
        utilities.add(r.get("utility", "unknown"))
        total_docs += _safe_int(r.get("doc_count", 0)) or 0
        total_tariff += _safe_int(r.get("tariff_sheet_count", 0)) or 0

    return {
        "summary": {
            "count": len(rows),
            "total_count": total_count,
            "unique_dockets": len(dockets),
            "unique_years": len(years),
            "year_range": f"{min(years)}-{max(years)}" if years else "N/A",
            "total_docs": total_docs,
            "total_tariff_sheets": total_tariff,
        },
        "rows": [
            {
                "docket_number": r.get("docket_number", "unknown"),
                "utility": r.get("utility", "unknown"),
                "year": _safe_int(r.get("year")),
                "doc_count": _safe_int(r.get("doc_count", 0)),
                "tariff_sheet_count": _safe_int(r.get("tariff_sheet_count", 0)),
                "rider_count": _safe_int(r.get("rider_count", 0)),
                "order_count": _safe_int(r.get("order_count", 0)),
                "cover_letter_count": _safe_int(r.get("cover_letter_count", 0)),
                "unknown_count": _safe_int(r.get("unknown_count", 0)),
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# Master builder
# ---------------------------------------------------------------------------

def build_database_intelligence_report(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    family_key: str | None = None,
    docket: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Run all deterministic sub-reports and return a unified dict.

    This is the single entry point for the CLI, LLM summarization, and
    overnight loop.
    """
    report = {
        "generated_at": _iso_now(),
        "config": {
            "limit": limit,
            "family_key": family_key,
            "docket": docket,
            "since": since,
        },
    }

    sections = [
        ("missing_versions", find_missing_versions),
        ("unknown_documents", find_unknown_documents),
        ("low_quality_parses", find_low_quality_parses),
        ("stale_artifacts", find_stale_artifacts),
        ("duplicate_documents", find_duplicate_documents),
        ("family_lineage_gaps", find_family_lineage_gaps),
        ("docket_coverage", find_docket_coverage_summary),
    ]

    summary_counts: dict[str, int] = {}

    for name, func in sections:
        # Build kwargs based on what the function accepts
        kwargs: dict[str, Any] = {"conn": conn, "limit": limit}
        if family_key:
            kwargs["family_key"] = family_key
        if docket:
            kwargs["docket"] = docket
        if since:
            kwargs["since"] = since

        try:
            result = func(**kwargs)
            report[name] = result
            summary = result.get("summary", {}) or {}
            # Prefer the unbounded ``total_count`` (real backlog size).
            # Fall back to ``count`` (LIMIT-capped row count) for any
            # sub-report that has not yet been migrated.
            summary_counts[name] = summary.get(
                "total_count",
                summary.get("count", 0),
            )
        except Exception as exc:
            report[name] = {
                "summary": {"count": 0, "error": str(exc)},
                "rows": [],
            }
            summary_counts[name] = 0

    report["summary_counts"] = summary_counts
    report["total_findings"] = sum(summary_counts.values())
    report["outcome_metrics"] = _outcome_metrics(conn)

    return report


def _outcome_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    """Outcome metrics: numbers the user actually cares about.

    Unlike ``summary_counts`` (problems shrinking toward zero), these
    are positive metrics that should grow as the loop succeeds:
    charges in the database, versions with charges, evidence coverage.
    Used by the loop to detect real progress that ``summary_counts``
    deltas might miss (e.g. dedup leaves the duplicate count unchanged
    because the SQL counts groups, but charge_count went up because
    the survivor now owns merged rows).
    """
    out: dict[str, Any] = {}

    def _scalar(sql: str, params: tuple = ()) -> int:
        try:
            row = conn.execute(sql, params).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        except sqlite3.Error:
            return 0

    out["tariff_charges_total"] = _scalar(
        """
        SELECT COUNT(*)
        FROM tariff_charges tc
        JOIN tariff_versions tv ON tv.id = tc.version_id
        JOIN tariff_families tf ON tf.family_key = tv.family_key
        WHERE tf.state = 'NC'
        """
    )
    linked_versions = _scalar(
        """
        SELECT COUNT(*)
        FROM tariff_versions tv
        JOIN historical_documents hd ON hd.id = tv.historical_document_id
        WHERE hd.state = 'NC'
        """
    )
    versions_with_charges = _scalar(
        """
        SELECT COUNT(DISTINCT tv.id)
        FROM tariff_versions tv
        JOIN historical_documents hd ON hd.id = tv.historical_document_id
        JOIN tariff_charges tc ON tc.version_id = tv.id
        WHERE hd.state = 'NC'
        """
    )
    historical_docs = _scalar(
        "SELECT COUNT(*) FROM historical_documents WHERE state = 'NC'"
    )
    docs_with_evidence = _scalar(
        """
        SELECT COUNT(*)
        FROM historical_documents
        WHERE state = 'NC'
          AND evidence_json IS NOT NULL
          AND evidence_json != ''
          AND evidence_json != '{}'
        """
    )
    out["linked_versions"] = linked_versions
    out["versions_with_charges"] = versions_with_charges
    out["extraction_coverage_pct"] = (
        round(100.0 * versions_with_charges / linked_versions, 2)
        if linked_versions else 0.0
    )
    out["historical_documents"] = historical_docs
    out["docs_with_evidence"] = docs_with_evidence
    out["evidence_coverage_pct"] = (
        round(100.0 * docs_with_evidence / historical_docs, 2)
        if historical_docs else 0.0
    )
    return out


# ---------------------------------------------------------------------------
# Docket gap recommender
# ---------------------------------------------------------------------------


def find_missing_docket_coverage(
    conn: sqlite3.Connection,
    *,
    limit: int = 25,
    utility: str | None = None,
    min_year: int | None = None,
    docket: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Recommend dockets with discovery records but low or zero processed coverage.

    Cross-references ``ncuc_discovery_records`` against ``tariff_versions`` and
    ``historical_documents`` by docket number.  Dockets with many discovery
    records but few historical documents are the highest-value targets.

    Also surfaces discovery records whose ``fetch_status != 'complete'``
    (never downloaded) and docket leads from ``regulatory_docket_leads`` that
    have no corresponding discovery records yet.
    """
    _ = since  # reserved for future use

    where = ""
    params: list[Any] = []
    if utility:
        where += " AND LOWER(ndr.utility) LIKE LOWER(?)"
        params.append(f"%{utility}%")
    if min_year:
        where += " AND CAST(SUBSTR(ndr.filing_date, 1, 4) AS INTEGER) >= ?"
        params.append(min_year)
    if docket:
        where += " AND ndr.docket_number = ?"
        params.append(docket)

    # ── Main coverage ranking ──────────────────────────────────────────
    coverage_sql = f"""
        SELECT
            ndr.docket_number,
            MAX(ndr.utility) AS utility,
            MAX(ndr.filing_title) AS filing_title,
            COUNT(DISTINCT ndr.id) AS discovery_records_count,
            COUNT(DISTINCT hd.id) AS historical_docs_count,
            COUNT(DISTINCT tv.id) AS tariff_versions_count,
            COUNT(DISTINCT tc.id) AS tariff_charges_count,
            SUM(CASE WHEN ndr.fetch_status = 'complete' THEN 1 ELSE 0 END) AS fetch_complete_count,
            SUM(CASE WHEN ndr.local_path IS NOT NULL AND ndr.local_path != '' THEN 1 ELSE 0 END) AS downloaded_count,
            COUNT(DISTINCT CASE WHEN dc.label = 'UNKNOWN' THEN hd.id END) AS unknown_classified_count
        FROM ncuc_discovery_records ndr
        LEFT JOIN tariff_versions tv ON tv.docket_number = ndr.docket_number
        LEFT JOIN historical_documents hd ON hd.id = tv.historical_document_id
        LEFT JOIN tariff_charges tc ON tc.version_id = tv.id
        LEFT JOIN document_classifications dc
            ON dc.subject_kind = 'historical_document'
            AND CAST(dc.subject_id AS INTEGER) = hd.id
            AND dc.stage = 'document_type'
            AND dc.superseded_by IS NULL
        WHERE ndr.docket_number IS NOT NULL
          AND ndr.docket_number != ''
          {where}
        GROUP BY ndr.docket_number
        ORDER BY discovery_records_count DESC, historical_docs_count ASC
    """

    rows = _query_rows(conn, coverage_sql, tuple(params), limit=limit)

    recommendations: list[dict[str, Any]] = []
    for r in rows:
        disc_count = _safe_int(r.get("discovery_records_count", 0)) or 0
        hd_count = _safe_int(r.get("historical_docs_count", 0)) or 0
        unk_count = _safe_int(r.get("unknown_classified_count", 0)) or 0
        fetch_complete = _safe_int(r.get("fetch_complete_count", 0)) or 0
        downloaded = _safe_int(r.get("downloaded_count", 0)) or 0

        # Coverage: if no discovery records → 100% (nothing to fetch)
        coverage_pct = round(100 * hd_count / disc_count, 1) if disc_count > 0 else 100.0

        # Recommended action
        if fetch_complete < disc_count and downloaded == 0:
            action = "fetch"
        elif hd_count == 0 and downloaded > 0:
            action = "import"
        elif unk_count > 0 and hd_count > 0 and (unk_count >= hd_count * 0.5):
            action = "classify"
        elif hd_count > 0 and disc_count > hd_count:
            action = "reprocess"
        else:
            action = "investigate"

        recommendations.append({
            "docket_number": r.get("docket_number", "unknown"),
            "utility": r.get("utility", "unknown"),
            "filing_title": r.get("filing_title", ""),
            "discovery_records_count": disc_count,
            "historical_docs_count": hd_count,
            "tariff_versions_count": _safe_int(r.get("tariff_versions_count", 0)) or 0,
            "tariff_charges_count": _safe_int(r.get("tariff_charges_count", 0)) or 0,
            "fetch_complete_count": fetch_complete,
            "downloaded_count": downloaded,
            "unknown_classified_count": unk_count,
            "coverage_pct": coverage_pct,
            "recommended_action": action,
        })

    # ── Docket leads with no discovery records ─────────────────────────
    leads_sql = """
        SELECT
            rdl.docket_number,
            rdl.utility,
            rdl.title,
            rdl.evidence_source,
            rdl.evidence_source_type,
            rdl.confidence_score,
            COUNT(*) OVER (PARTITION BY rdl.docket_number) AS lead_count
        FROM regulatory_docket_leads rdl
        WHERE rdl.docket_number IS NOT NULL
          AND rdl.docket_number != ''
          AND rdl.docket_number NOT IN (
              SELECT DISTINCT ndr.docket_number FROM ncuc_discovery_records ndr
              WHERE ndr.docket_number IS NOT NULL AND ndr.docket_number != ''
          )
          AND rdl.docket_number NOT IN (
              SELECT DISTINCT tv.docket_number FROM tariff_versions tv
              WHERE tv.docket_number IS NOT NULL AND tv.docket_number != ''
          )
        ORDER BY lead_count DESC, rdl.confidence_score DESC
    """

    lead_rows = _query_rows(conn, leads_sql, (), limit=limit)
    leads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for lr in lead_rows:
        dkt = lr.get("docket_number", "")
        if dkt in seen:
            continue
        seen.add(dkt)
        leads.append({
            "docket_number": dkt,
            "utility": lr.get("utility", ""),
            "title": lr.get("title", ""),
            "evidence_source": lr.get("evidence_source", ""),
            "lead_count": _safe_int(lr.get("lead_count", 0)) or 0,
            "confidence_score": lr.get("confidence_score"),
        })

    # ── Unfetched discovery records ────────────────────────────────────
    unfetched_sql = f"""
        SELECT
            ndr.docket_number,
            ndr.utility,
            ndr.filing_title,
            COUNT(*) AS unfetched_count,
            SUM(CASE WHEN ndr.local_path IS NOT NULL AND ndr.local_path != '' THEN 1 ELSE 0 END) AS downloaded_but_not_imported
        FROM ncuc_discovery_records ndr
        WHERE ndr.docket_number IS NOT NULL
          AND ndr.docket_number != ''
          AND ndr.fetch_status != 'complete'
          {where}
        GROUP BY ndr.docket_number
        ORDER BY unfetched_count DESC
    """

    unfetched_rows = _query_rows(conn, unfetched_sql, tuple(params), limit=limit)
    unfetched: list[dict[str, Any]] = []
    for uf in unfetched_rows:
        unfetched.append({
            "docket_number": uf.get("docket_number", "unknown"),
            "utility": uf.get("utility", "unknown"),
            "filing_title": uf.get("filing_title", ""),
            "unfetched_count": _safe_int(uf.get("unfetched_count", 0)) or 0,
            "downloaded_but_not_imported": _safe_int(uf.get("downloaded_but_not_imported", 0)) or 0,
        })

    return {
        "summary": {
            "count": len(recommendations),
            "dockets_with_zero_coverage": sum(1 for r in recommendations if r["coverage_pct"] == 0),
            "dockets_with_low_coverage": sum(1 for r in recommendations if 0 < r["coverage_pct"] < 50),
            "total_recommendations": len(recommendations),
            "leads_without_discovery": len(leads),
            "unfetched_dockets": len(unfetched),
        },
        "recommendations": recommendations,
        "docket_leads": leads,
        "unfetched_dockets": unfetched,
    }
