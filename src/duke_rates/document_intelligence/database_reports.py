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
    """Find historical documents the bootstrap command can version-link.

    This category is wired to ``bootstrap-missing-versions-nc``. Keep the
    report definition aligned with that command: NC historical documents with
    a date and path but no ``tariff_versions.historical_document_id`` link.
    Annual timeline gaps are a different audit surface and are not directly
    fixed by the bootstrap action.
    """
    sql = """
        SELECT
            hd.id AS historical_document_id,
            hd.family_key,
            hd.company,
            hd.effective_start,
            hd.title,
            hd.local_path AS source_pdf
        FROM historical_documents hd
        WHERE hd.state = 'NC'
          AND hd.company IN ('progress', 'carolinas')
          AND hd.effective_start IS NOT NULL
          AND hd.effective_start != ''
          AND hd.local_path IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM tariff_versions tv
              WHERE tv.historical_document_id = hd.id
          )
    """

    params: tuple = ()
    if family_key:
        sql += " AND hd.family_key = ?"
        params = (family_key,)

    total_count = _count_rows(conn, sql, params)
    sql += " ORDER BY hd.family_key, hd.effective_start, hd.id"
    rows = _query_rows(conn, sql, params, limit=limit)

    parsed_rows = [
        {
            "historical_document_id": _safe_int(r.get("historical_document_id")),
            "family_key": r.get("family_key", ""),
            "company": r.get("company", ""),
            "effective_start": r.get("effective_start", ""),
            "title": r.get("title", ""),
            "source_pdf": r.get("source_pdf", ""),
        }
        for r in rows
    ]
    family_counts: dict[str, int] = {}
    for row in parsed_rows:
        family = str(row.get("family_key") or "")
        family_counts[family] = family_counts.get(family, 0) + 1

    return {
        "summary": {
            "count": len(parsed_rows),
            "total_count": total_count,
            "historical_docs_missing_versions": total_count,
            "families_in_sample": len(family_counts),
            "by_family": dict(sorted(family_counts.items(), key=lambda item: (-item[1], item[0]))[:10]),
        },
        "rows": parsed_rows,
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
    """Find documents the LLM adjudication action can process.

    This category is wired to ``doc-intel adjudicate-classifications``, so it
    mirrors that command's candidate definition: active rule v1 and embedding
    KNN document-type rows that disagree, contain UNKNOWN, or are low
    confidence, with no active LLM classification already present.
    """
    sql = """
        WITH adjudication_candidates AS (
            SELECT
                r.subject_kind,
                r.subject_id,
                r.label AS rule_label,
                r.confidence AS rule_confidence,
                r.classifier_version AS rule_classifier_version,
                r.created_at AS rule_created_at,
                e.label AS embedding_label,
                e.confidence AS embedding_confidence,
                e.classifier_version AS embedding_classifier_version,
                e.created_at AS embedding_created_at
            FROM document_classifications r
            JOIN document_classifications e
              ON e.subject_kind = r.subject_kind
             AND e.subject_id = r.subject_id
             AND e.stage = r.stage
             AND e.classifier = 'embedding_knn_v1'
             AND e.superseded_by IS NULL
            LEFT JOIN document_classifications existing_llm
              ON existing_llm.subject_kind = r.subject_kind
             AND existing_llm.subject_id = r.subject_id
             AND existing_llm.stage = r.stage
             AND existing_llm.classifier LIKE 'llm_%'
             AND existing_llm.superseded_by IS NULL
            WHERE r.stage = 'document_type'
              AND r.classifier = 'rule_document_type_v1'
              AND r.superseded_by IS NULL
              AND existing_llm.id IS NULL
              AND (
                  r.label != e.label
                  OR r.label = 'UNKNOWN'
                  OR e.label = 'UNKNOWN'
                  OR MAX(r.confidence, e.confidence) < 0.5
              )
        ),
        fingerprint_one AS (
            SELECT
                source_pdf,
                COALESCE(MAX(cluster_signature_v1), 'no_cluster') AS cluster_signature
            FROM document_fingerprints_v2
            GROUP BY source_pdf
        ),
        unknown_documents AS (
            SELECT
                ac.subject_kind,
                ac.subject_id,
                MAX(ac.rule_confidence, ac.embedding_confidence) AS confidence,
                ac.rule_label,
                ac.embedding_label,
                ac.rule_classifier_version,
                ac.embedding_classifier_version,
                MAX(ac.rule_created_at, ac.embedding_created_at) AS last_classified_at,
                COALESCE(fp.cluster_signature, 'no_cluster') AS cluster_signature,
                COALESCE(hd.title, '') AS title,
                hd.local_path AS source_pdf,
                hd.family_key,
                hd.effective_start
            FROM adjudication_candidates ac
            LEFT JOIN historical_documents hd
                ON ac.subject_kind = 'historical_document'
                AND CAST(ac.subject_id AS INTEGER) = hd.id
            LEFT JOIN fingerprint_one fp
                ON fp.source_pdf = hd.local_path
            WHERE 1 = 1
    """

    params: tuple = ()
    if docket:
        sql += " AND hd.family_key IN (SELECT family_key FROM tariff_versions WHERE docket_number LIKE ?)"
        params = (f"%{docket}%",)
    if since:
        sql += " AND MAX(ac.rule_created_at, ac.embedding_created_at) >= ?"
        params = params + (since,)

    sql += """
        )
        SELECT
            subject_kind,
            subject_id,
            confidence,
            rule_label,
            embedding_label,
            rule_classifier_version,
            embedding_classifier_version,
            cluster_signature,
            COUNT(*) OVER (PARTITION BY cluster_signature) AS cluster_size,
            title,
            source_pdf,
            family_key,
            effective_start
        FROM unknown_documents
    """

    total_count = _count_rows(conn, sql, params)
    sql += " ORDER BY cluster_size DESC, subject_id"

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
            "classifier": "rule_document_type_v1,embedding_knn_v1",
            "rule_label": r.get("rule_label", ""),
            "embedding_label": r.get("embedding_label", ""),
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
    #
    # Only rows that can be mapped to historical_documents are actionable
    # by the reprocess queue. Large batches of tiered ingest attempts can
    # exist for source paths that are not historical documents; counting
    # them here makes the autonomous loop chase work it cannot enqueue.
    # Likewise, any document already present in historical_reprocess_queue
    # has already been routed through the corrective path, regardless of
    # status, so it should not keep inflating the candidate count.
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
        JOIN historical_documents hd
            ON REPLACE(COALESCE(hd.local_path, ''), '\\', '/')
             = REPLACE(COALESCE(pal.source_pdf, ''), '\\', '/')
        WHERE pal.rn = 1
          AND (pal.charge_count = 0
               OR pal.confidence < 0.3
               OR pal.status IN ('empty', 'error'))
          AND COALESCE(pal.status, '') NOT LIKE 'skipped_%'
          AND COALESCE(hd.effective_start, '') != ''
          AND hd.family_key NOT LIKE '%-doc-%'
          AND hd.family_key NOT LIKE '%-program-%'
          AND COALESCE(pal.parser_profile, '') NOT IN ('unknown', 'tiered_ingest')
          AND NOT EXISTS (
              SELECT 1
              FROM historical_reprocess_queue hrq
              WHERE hrq.historical_document_id = hd.id
                 OR REPLACE(COALESCE(hrq.source_pdf, ''), '\\', '/')
                  = REPLACE(COALESCE(pal.source_pdf, ''), '\\', '/')
          )
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
    """Find actionable stale artifact work.

    The ``no_evidence_json`` branch mirrors
    ``Repository.backfill_evidence_json`` so the autonomous loop only sees
    evidence rows the wired corrective action can actually backfill. Missing
    evidence without a usable span-artifact breakdown needs a different repair
    path and should not keep this category hot.
    """
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
          AND hd.content_hash IS NOT NULL
          AND hd.content_hash != ''
          AND (hd.evidence_json IS NULL OR hd.evidence_json = '{}' OR hd.evidence_json = '')
          AND EXISTS (
              SELECT 1 FROM ncuc_span_artifacts nsa
              WHERE nsa.file_hash = hd.content_hash
                AND nsa.evidence_score_breakdown_json IS NOT NULL
                AND nsa.evidence_score_breakdown_json != ''
                AND nsa.evidence_score_breakdown_json != '{}'
          )
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

    A full-PDF ``content_hash`` alone is not enough: a single PDF can be
    split into many span-scoped historical documents that legitimately share
    that hash. Match the same scope used by ``deduplicate-documents-nc``:
    hash, family, start page, and end page.
    """
    sql = """
        SELECT
            hd.content_hash,
            hd.family_key,
            COALESCE(hd.start_page, -1) AS start_page_scope,
            COALESCE(hd.end_page, COALESCE(hd.start_page, -1)) AS end_page_scope,
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
        GROUP BY
            hd.content_hash,
            hd.family_key,
            COALESCE(hd.start_page, -1),
            COALESCE(hd.end_page, COALESCE(hd.start_page, -1))
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
            "family_key": r.get("family_key", ""),
            "start_page": None if r.get("start_page_scope") == -1 else _safe_int(r.get("start_page_scope")),
            "end_page": None if r.get("end_page_scope") == -1 else _safe_int(r.get("end_page_scope")),
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

    # Gap type 2: docs with null effective_start that the deterministic repair
    # command can resolve from same-PDF siblings with one known date.
    sql2 = """
        WITH pdf_dates AS (
            SELECT
                local_path,
                COUNT(DISTINCT effective_start) AS known_date_count,
                MAX(effective_start) AS inferred_effective_start
            FROM historical_documents
            WHERE state = 'NC'
              AND local_path IS NOT NULL
              AND COALESCE(effective_start, '') <> ''
              AND SUBSTR(effective_start, 1, 4) GLOB '[12][0-9][0-9][0-9]'
            GROUP BY local_path
        )
        SELECT
            hd.id AS historical_document_id,
            hd.family_key,
            hd.company,
            pdf_dates.inferred_effective_start AS effective_start,
            hd.title,
            hd.local_path AS source_pdf,
            'no_effective_start' AS gap_type
        FROM historical_documents hd
        JOIN pdf_dates
          ON pdf_dates.local_path = hd.local_path
        WHERE hd.state = 'NC'
          AND hd.local_path IS NOT NULL
          AND (hd.effective_start IS NULL OR hd.effective_start = '')
          AND pdf_dates.known_date_count = 1
          AND NOT EXISTS (
              SELECT 1
              FROM tariff_versions linked_tv
              WHERE linked_tv.family_key = hd.family_key
                AND linked_tv.effective_start = pdf_dates.inferred_effective_start
                AND linked_tv.historical_document_id IS NOT NULL
          )
          AND (
              NOT EXISTS (
                  SELECT 1
                  FROM tariff_versions any_tv
                  WHERE any_tv.family_key = hd.family_key
                    AND any_tv.effective_start = pdf_dates.inferred_effective_start
              )
              OR 1 = (
                  SELECT COUNT(*)
                  FROM tariff_versions unlinked_tv
                  WHERE unlinked_tv.family_key = hd.family_key
                    AND unlinked_tv.effective_start = pdf_dates.inferred_effective_start
                    AND unlinked_tv.historical_document_id IS NULL
              )
          )
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
          AND (tv.source_type != 'utility_current' OR tv.document_id IS NULL)
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
    """Return actionable docket coverage work for the autonomous loop.

    The old docket/year summary was descriptive, but it inflated the loop's
    backlog with already-processed buckets. The loop-facing category should
    reflect work the acquisition/import lanes can actually move.
    """
    _ = since
    report = find_missing_docket_coverage(conn, limit=limit, docket=docket)
    rows = list(report.get("recommendations", []))
    leads = list(report.get("docket_leads", []))
    fetch_records = sum(_safe_int(row.get("fetch_eligible_count", 0)) or 0 for row in rows)
    import_records = sum(_safe_int(row.get("downloaded_not_imported_count", 0)) or 0 for row in rows)
    return {
        "summary": {
            "count": len(rows) + len(leads),
            "total_count": len(rows) + len(leads),
            "fetch_eligible_records": fetch_records,
            "downloaded_not_imported_records": import_records,
            "dockets_requiring_fetch": sum(
                1 for row in rows
                if (_safe_int(row.get("fetch_eligible_count", 0)) or 0) > 0
            ),
            "dockets_requiring_import": sum(
                1 for row in rows
                if (_safe_int(row.get("downloaded_not_imported_count", 0)) or 0) > 0
            ),
            "leads_without_discovery": len(leads),
        },
        "rows": rows,
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

    # NOTE: join via historical_documents.state, NOT tariff_families.state.
    # Some NC versions reference a family_key that has no row in
    # tariff_families (e.g. provisional families retired before the
    # next discovery sweep). Joining tariff_families would silently
    # drop those versions' charges. The 2026-05-23 overnight under-
    # counted by 670 charges this way, triggering a premature
    # no_improvement stop. All other metrics in this function already
    # use the historical_documents join — make this one consistent.
    out["tariff_charges_total"] = _scalar(
        """
        SELECT COUNT(*)
        FROM tariff_charges tc
        JOIN tariff_versions tv ON tv.id = tc.version_id
        JOIN historical_documents hd ON hd.id = tv.historical_document_id
        WHERE hd.state = 'NC'
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
    """Recommend actionable docket acquisition/import work.

    Each discovery record contributes once. Imported coverage is inferred by
    matching tariff versions at the docket level. Import work mirrors
    ``Repository.list_ncuc_pending_imports()``: successfully fetched discovery
    rows with no ``ncuc_span_artifacts`` rows. Fetch work uses the real live
    statuses (``pending``, ``failed``, ``requires_browser``), not the older
    ``complete`` sentinel.
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
        WITH discovery_rows AS (
            SELECT
                ndr.id,
                ndr.docket_number,
                ndr.utility,
                ndr.filing_title,
                ndr.fetch_status,
                ndr.local_path,
                ndr.content_hash,
                ndr.error_detail
            FROM ncuc_discovery_records ndr
            WHERE ndr.docket_number IS NOT NULL
              AND ndr.docket_number != ''
              {where}
        ),
        docket_rollup AS (
            SELECT
                docket_number,
                MAX(utility) AS utility,
                MAX(filing_title) AS filing_title,
                COUNT(*) AS discovery_records_count,
                SUM(CASE
                    WHEN fetch_status IN ('pending', 'failed', 'requires_browser')
                      OR (fetch_status IS NULL AND (local_path IS NULL OR local_path = ''))
                    THEN 1 ELSE 0
                END) AS fetch_eligible_count,
                SUM(CASE WHEN local_path IS NOT NULL AND local_path != '' THEN 1 ELSE 0 END) AS downloaded_count,
                SUM(CASE
                    WHEN local_path IS NOT NULL
                     AND local_path != ''
                     AND fetch_status = 'success'
                     AND COALESCE(error_detail, '') NOT LIKE 'import_skipped_%'
                     AND COALESCE(error_detail, '') NOT LIKE 'import_failed_%'
                     AND NOT EXISTS (
                         SELECT 1
                         FROM ncuc_span_artifacts nsa
                         WHERE nsa.discovery_record_id = discovery_rows.id
                     )
                    THEN 1 ELSE 0
                END) AS downloaded_not_imported_count
            FROM discovery_rows
            GROUP BY docket_number
        ),
        version_rollup AS (
            SELECT
                tv.docket_number,
                COUNT(DISTINCT tv.historical_document_id) AS historical_docs_count,
                COUNT(DISTINCT tv.id) AS tariff_versions_count,
                COUNT(DISTINCT tc.id) AS tariff_charges_count,
                COUNT(DISTINCT CASE WHEN dc.label = 'UNKNOWN' THEN tv.historical_document_id END) AS unknown_classified_count
            FROM tariff_versions tv
            LEFT JOIN tariff_charges tc
              ON tc.version_id = tv.id
            LEFT JOIN document_classifications dc
              ON dc.subject_kind = 'historical_document'
             AND CAST(dc.subject_id AS INTEGER) = tv.historical_document_id
             AND dc.stage = 'document_type'
             AND dc.superseded_by IS NULL
            WHERE tv.docket_number IS NOT NULL
              AND tv.docket_number != ''
            GROUP BY tv.docket_number
        )
        SELECT
            dr.docket_number,
            dr.utility,
            dr.filing_title,
            dr.discovery_records_count,
            COALESCE(vr.historical_docs_count, 0) AS historical_docs_count,
            COALESCE(vr.tariff_versions_count, 0) AS tariff_versions_count,
            COALESCE(vr.tariff_charges_count, 0) AS tariff_charges_count,
            dr.fetch_eligible_count,
            dr.downloaded_count,
            dr.downloaded_not_imported_count,
            COALESCE(vr.unknown_classified_count, 0) AS unknown_classified_count
        FROM docket_rollup dr
        LEFT JOIN version_rollup vr
          ON vr.docket_number = dr.docket_number
        WHERE dr.fetch_eligible_count > 0
           OR dr.downloaded_not_imported_count > 0
           OR COALESCE(vr.unknown_classified_count, 0) > 0
        ORDER BY
            dr.downloaded_not_imported_count DESC,
            dr.fetch_eligible_count DESC,
            dr.discovery_records_count DESC
    """

    rows = _query_rows(conn, coverage_sql, tuple(params), limit=limit)

    recommendations: list[dict[str, Any]] = []
    for r in rows:
        disc_count = _safe_int(r.get("discovery_records_count", 0)) or 0
        hd_count = _safe_int(r.get("historical_docs_count", 0)) or 0
        unk_count = _safe_int(r.get("unknown_classified_count", 0)) or 0
        fetch_eligible = _safe_int(r.get("fetch_eligible_count", 0)) or 0
        downloaded = _safe_int(r.get("downloaded_count", 0)) or 0
        downloaded_not_imported = _safe_int(r.get("downloaded_not_imported_count", 0)) or 0

        # Coverage: if no discovery records → 100% (nothing to fetch)
        coverage_pct = round(100 * hd_count / disc_count, 1) if disc_count > 0 else 100.0

        # Recommended action
        if downloaded_not_imported > 0:
            action = "import"
        elif fetch_eligible > 0:
            action = "fetch"
        elif unk_count > 0 and hd_count > 0 and (unk_count >= hd_count * 0.5):
            action = "classify"
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
            "fetch_eligible_count": fetch_eligible,
            "downloaded_count": downloaded,
            "downloaded_not_imported_count": downloaded_not_imported,
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
          AND (
              ndr.fetch_status IN ('pending', 'failed', 'requires_browser')
              OR (ndr.fetch_status IS NULL AND (ndr.local_path IS NULL OR ndr.local_path = ''))
          )
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
