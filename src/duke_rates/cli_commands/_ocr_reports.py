"""Shared OCR report helpers.

Used by the `ocr` sub-app (`cli_commands/ocr.py`) and by document-intelligence
commands still in `cli.py` (e.g., `validate-document-diagnostics`,
`_build_fast_ocr_remediation_summary_nc`). Keeping these helpers here avoids a
circular import between cli.py and cli_commands/ocr.py.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any


def _safe_text_file_length(path_value: object) -> int:
    path_text = str(path_value or "").strip()
    if not path_text:
        return 0
    try:
        path = Path(path_text)
        if not path.exists() or not path.is_file():
            return 0
        return len(path.read_text(encoding="utf-8", errors="ignore").strip())
    except Exception:
        return 0


def _classify_ocr_route(
    *,
    raw_text_chars: int,
    outcome_quality: str,
    parser_profile: str,
    page_count: int,
    title: str,
    has_ocr_artifact: bool,
    stale_reasons: list[str],
) -> tuple[str, str]:
    lowered_title = title.lower()
    layout_heavy = (
        page_count >= 5
        or "summary" in lowered_title
        or "compliance" in lowered_title
        or "book" in lowered_title
    )
    if raw_text_chars == 0 and parser_profile == "unknown":
        return (
            "no_usable_text_unknown_profile",
            "run_docling_or_paddle_structure" if layout_heavy else "queue_ocr_or_paddle",
        )
    if raw_text_chars == 0:
        return (
            "no_usable_text",
            "run_docling_or_paddle_structure" if layout_heavy else "queue_ocr_or_paddle",
        )
    if outcome_quality in {"weak", "empty"} and not has_ocr_artifact:
        return ("weak_without_ocr", "queue_ocr_or_paddle")
    if outcome_quality in {"weak", "empty"} and layout_heavy:
        return ("weak_layout_sensitive", "run_docling_or_paddle_structure")
    if outcome_quality in {"weak", "empty"}:
        return ("weak_after_text_recovery", "parser_or_page_level_glm_review")
    if stale_reasons:
        return ("stale_artifacts", "reprocess_or_refresh_ocr")
    return ("healthy_or_non_ocr_issue", "no_ocr_action")


def _build_ocr_benchmark_nc_report(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    backend_filter: str | None = None,
    outcome_filter: str | None = None,
    needs_review_only: bool = False,
    stale_only: bool = False,
    sort_by: str = "recent",
) -> dict[str, object]:
    from duke_rates.db.reprocess import find_stale_historical_documents

    stale_rows = find_stale_historical_documents(conn, limit=max(limit * 5, 100))
    stale_by_document_id = {
        int(item["historical_document_id"]): list(item.get("reasons") or [])
        for item in stale_rows
    }
    rows = conn.execute(
        """
        WITH latest_ocr AS (
            SELECT oa.*
            FROM ocr_artifacts oa
            JOIN (
                SELECT source_pdf, MAX(id) AS max_id
                FROM ocr_artifacts
                GROUP BY source_pdf
            ) latest
              ON latest.max_id = oa.id
        ),
        ocr_docs AS (
            SELECT
                hd.id AS historical_document_id,
                hd.family_key,
                hd.company,
                hd.title,
                hd.local_path,
                hd.raw_text_path,
                hd.content_hash,
                lo.backend,
                lo.status AS ocr_status,
                lo.page_count,
                lo.ocr_confidence,
                lo.metadata_json AS ocr_metadata_json
            FROM historical_documents hd
            JOIN latest_ocr lo
              ON lo.source_pdf = hd.local_path
            WHERE hd.state = 'NC'
        ),
        latest_runs AS (
            SELECT hpr.*
            FROM historical_processing_runs hpr
            JOIN (
                SELECT historical_document_id, MAX(id) AS max_id
                FROM historical_processing_runs
                WHERE historical_document_id IN (SELECT historical_document_id FROM ocr_docs)
                GROUP BY historical_document_id
            ) latest
              ON latest.max_id = hpr.id
        ),
        latest_page_artifacts AS (
            SELECT pa.*
            FROM ncuc_page_artifacts pa
            JOIN (
                SELECT source_pdf, file_hash, MAX(id) AS max_id
                FROM ncuc_page_artifacts
                WHERE source_pdf IN (SELECT local_path FROM ocr_docs)
                GROUP BY source_pdf, file_hash
            ) latest
              ON latest.max_id = pa.id
        ),
        latest_span_artifacts AS (
            SELECT sa.*
            FROM ncuc_span_artifacts sa
            JOIN (
                SELECT source_pdf, file_hash, MAX(id) AS max_id
                FROM ncuc_span_artifacts
                WHERE source_pdf IN (SELECT local_path FROM ocr_docs)
                GROUP BY source_pdf, file_hash
            ) latest
              ON latest.max_id = sa.id
        ),
        latest_parse_attempts AS (
            SELECT pal.*
            FROM parse_attempt_logs pal
            JOIN (
                SELECT CAST(json_extract(metadata_json, '$.historical_document_id') AS INTEGER) AS historical_document_id,
                       MAX(id) AS max_id
                FROM parse_attempt_logs
                WHERE json_extract(metadata_json, '$.historical_document_id') IS NOT NULL
                  AND CAST(json_extract(metadata_json, '$.historical_document_id') AS INTEGER)
                      IN (SELECT historical_document_id FROM ocr_docs)
                GROUP BY CAST(json_extract(metadata_json, '$.historical_document_id') AS INTEGER)
            ) latest
              ON latest.max_id = pal.id
        ),
        latest_reviews AS (
            SELECT pro.*
            FROM parse_review_outcomes pro
            JOIN (
                SELECT parse_attempt_id, MAX(id) AS max_id
                FROM parse_review_outcomes
                WHERE parse_attempt_id IS NOT NULL
                GROUP BY parse_attempt_id
            ) latest
              ON latest.max_id = pro.id
        )
        SELECT
            od.historical_document_id,
            od.family_key,
            od.company,
            od.title,
            od.local_path,
            od.raw_text_path,
            od.content_hash,
            od.backend,
            od.ocr_status,
            od.page_count,
            od.ocr_confidence,
            od.ocr_metadata_json,
            lpa.artifact_version AS page_artifact_version,
            lpa.metadata_json AS page_metadata_json,
            lsa.artifact_version AS span_artifact_version,
            lr.status AS parse_status,
            lr.outcome_quality,
            lr.charge_count,
            lr.parser_profile,
            lpat.id AS parse_attempt_id,
            lrev.outcome AS review_outcome
        FROM ocr_docs od
        LEFT JOIN latest_runs lr
          ON lr.historical_document_id = od.historical_document_id
        LEFT JOIN latest_page_artifacts lpa
          ON lpa.source_pdf = od.local_path
         AND (lpa.file_hash IS od.content_hash OR lpa.file_hash = od.content_hash)
        LEFT JOIN latest_span_artifacts lsa
          ON lsa.source_pdf = od.local_path
         AND (lsa.file_hash IS od.content_hash OR lsa.file_hash = od.content_hash)
        LEFT JOIN latest_parse_attempts lpat
          ON CAST(json_extract(lpat.metadata_json, '$.historical_document_id') AS INTEGER) = od.historical_document_id
        LEFT JOIN latest_reviews lrev
          ON lrev.parse_attempt_id = lpat.id
        ORDER BY od.historical_document_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    report_rows: list[dict[str, object]] = []
    backend_counts: dict[str, int] = {}
    normalization_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}
    backend_outcome_counts: dict[tuple[str, str], int] = {}
    route_reason_counts: dict[str, int] = {}
    recommended_lane_counts: dict[str, int] = {}
    page_artifact_version_counts: dict[str, int] = {}
    span_artifact_version_counts: dict[str, int] = {}
    review_outcome_counts: dict[str, int] = {}

    for row in rows:
        ocr_metadata = json.loads(row["ocr_metadata_json"] or "{}")
        page_metadata = json.loads(row["page_metadata_json"] or "{}")
        backend = str(ocr_metadata.get("selected_backend") or row["backend"] or "unknown")
        normalization_version = str(ocr_metadata.get("ocr_normalization_version") or "unknown")
        outcome_quality = str(row["outcome_quality"] or "missing")
        page_artifact_version = str(row["page_artifact_version"] or "missing")
        span_artifact_version = str(row["span_artifact_version"] or "missing")
        review_outcome = str(row["review_outcome"] or "unreviewed")
        historical_document_id = int(row["historical_document_id"])
        stale_reasons = list(stale_by_document_id.get(historical_document_id) or [])
        raw_text_chars = _safe_text_file_length(row["raw_text_path"])
        route_reason, recommended_lane = _classify_ocr_route(
            raw_text_chars=raw_text_chars,
            outcome_quality=outcome_quality,
            parser_profile=str(row["parser_profile"] or "unknown"),
            page_count=int(row["page_count"] or 0),
            title=str(row["title"] or ""),
            has_ocr_artifact=bool(row["backend"]),
            stale_reasons=stale_reasons,
        )

        if backend_filter and backend != backend_filter:
            continue
        if outcome_filter and outcome_quality != outcome_filter:
            continue
        if needs_review_only and review_outcome != "needs_review":
            continue
        if stale_only and not stale_reasons:
            continue

        backend_counts[backend] = backend_counts.get(backend, 0) + 1
        normalization_counts[normalization_version] = normalization_counts.get(normalization_version, 0) + 1
        outcome_counts[outcome_quality] = outcome_counts.get(outcome_quality, 0) + 1
        backend_outcome_counts[(backend, outcome_quality)] = backend_outcome_counts.get((backend, outcome_quality), 0) + 1
        route_reason_counts[route_reason] = route_reason_counts.get(route_reason, 0) + 1
        recommended_lane_counts[recommended_lane] = recommended_lane_counts.get(recommended_lane, 0) + 1
        page_artifact_version_counts[page_artifact_version] = page_artifact_version_counts.get(page_artifact_version, 0) + 1
        span_artifact_version_counts[span_artifact_version] = span_artifact_version_counts.get(span_artifact_version, 0) + 1
        review_outcome_counts[review_outcome] = review_outcome_counts.get(review_outcome, 0) + 1

        report_rows.append(
            {
                "historical_document_id": row["historical_document_id"],
                "family_key": row["family_key"],
                "company": row["company"],
                "title": row["title"],
                "stale_reasons": stale_reasons,
                "backend": backend,
                "ocr_status": row["ocr_status"],
                "ocr_normalization_version": normalization_version,
                "attempted_backends": list(ocr_metadata.get("attempted_backends") or []),
                "page_count": int(row["page_count"] or 0),
                "raw_text_chars": raw_text_chars,
                "route_reason": route_reason,
                "recommended_lane": recommended_lane,
                "ocr_confidence": row["ocr_confidence"],
                "page_artifact_version": page_artifact_version,
                "span_artifact_version": span_artifact_version,
                "page_artifact_source": page_metadata.get("artifact_source"),
                "parse_status": row["parse_status"],
                "outcome_quality": outcome_quality,
                "charge_count": int(row["charge_count"] or 0),
                "parser_profile": row["parser_profile"],
                "review_outcome": review_outcome,
                "parse_attempt_id": row["parse_attempt_id"],
            }
        )

    backend_summary = [
        {"backend": key, "count": value}
        for key, value in sorted(backend_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    normalization_summary = [
        {"ocr_normalization_version": key, "count": value}
        for key, value in sorted(normalization_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    outcome_summary = [
        {"outcome_quality": key, "count": value}
        for key, value in sorted(outcome_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    route_reason_summary = [
        {"route_reason": key, "count": value}
        for key, value in sorted(route_reason_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    recommended_lane_summary = [
        {"recommended_lane": key, "count": value}
        for key, value in sorted(recommended_lane_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    page_artifact_version_summary = [
        {"page_artifact_version": key, "count": value}
        for key, value in sorted(page_artifact_version_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    span_artifact_version_summary = [
        {"span_artifact_version": key, "count": value}
        for key, value in sorted(span_artifact_version_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    review_outcome_summary = [
        {"review_outcome": key, "count": value}
        for key, value in sorted(review_outcome_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    backend_outcome_summary = [
        {"backend": backend, "outcome_quality": outcome, "count": count}
        for (backend, outcome), count in sorted(
            backend_outcome_counts.items(),
            key=lambda item: (-item[1], item[0][0], item[0][1]),
        )
    ]

    weak_rank = {"weak": 0, "missing": 1, "strong": 2}
    review_rank = {"needs_review": 0, "unreviewed": 1, "accepted": 2, "corrected": 3, "rejected": 4}
    if sort_by == "weak-first":
        report_rows.sort(
            key=lambda row: (
                weak_rank.get(str(row.get("outcome_quality") or "missing"), 9),
                -len(list(row.get("stale_reasons") or [])),
                int(row.get("historical_document_id") or 0),
            )
        )
    elif sort_by == "review-first":
        report_rows.sort(
            key=lambda row: (
                review_rank.get(str(row.get("review_outcome") or "unreviewed"), 9),
                weak_rank.get(str(row.get("outcome_quality") or "missing"), 9),
                int(row.get("historical_document_id") or 0),
            )
        )
    elif sort_by == "stale-first":
        report_rows.sort(
            key=lambda row: (
                0 if row.get("stale_reasons") else 1,
                -len(list(row.get("stale_reasons") or [])),
                weak_rank.get(str(row.get("outcome_quality") or "missing"), 9),
                int(row.get("historical_document_id") or 0),
            )
        )
    else:
        report_rows.sort(
            key=lambda row: -int(row.get("historical_document_id") or 0)
        )

    return {
        "row_count": len(report_rows),
        "backend_summary": backend_summary,
        "normalization_summary": normalization_summary,
        "outcome_summary": outcome_summary,
        "route_reason_summary": route_reason_summary,
        "recommended_lane_summary": recommended_lane_summary,
        "page_artifact_version_summary": page_artifact_version_summary,
        "span_artifact_version_summary": span_artifact_version_summary,
        "review_outcome_summary": review_outcome_summary,
        "backend_outcome_summary": backend_outcome_summary,
        "rows": report_rows,
    }


def _build_ocr_remediation_candidates_nc_report(
    conn: sqlite3.Connection,
    *,
    limit: int = 25,
    company: str | None = None,
    family_key: str | None = None,
) -> dict[str, Any]:
    from duke_rates.db.reprocess import find_stale_historical_documents

    stale_rows = find_stale_historical_documents(conn, limit=max(limit * 10, 250))
    stale_by_document_id = {
        int(item["historical_document_id"]): list(item.get("reasons") or [])
        for item in stale_rows
    }
    query = """
        WITH latest_runs AS (
            SELECT hpr.*
            FROM historical_processing_runs hpr
            JOIN (
                SELECT historical_document_id, MAX(id) AS max_id
                FROM historical_processing_runs
                WHERE historical_document_id IS NOT NULL
                GROUP BY historical_document_id
            ) latest
              ON latest.max_id = hpr.id
        ),
        latest_ocr AS (
            SELECT oa.*
            FROM ocr_artifacts oa
            JOIN (
                SELECT source_pdf, MAX(id) AS max_id
                FROM ocr_artifacts
                GROUP BY source_pdf
            ) latest
              ON latest.max_id = oa.id
        ),
        page_text AS (
            SELECT
                source_pdf,
                file_hash,
                SUM(text_length) AS page_artifact_text_chars
            FROM ncuc_page_artifacts
            GROUP BY source_pdf, file_hash
        )
        SELECT
            hd.id AS historical_document_id,
            hd.family_key,
            hd.company,
            hd.title,
            hd.local_path,
            hd.raw_text_path,
            hd.start_page,
            hd.end_page,
            lr.parser_profile,
            lr.outcome_quality,
            lr.charge_count,
            lo.backend AS ocr_backend,
            lo.status AS ocr_status,
            lo.page_count AS ocr_page_count,
            lo.ocr_confidence,
            COALESCE(pt.page_artifact_text_chars, 0) AS page_artifact_text_chars
        FROM historical_documents hd
        LEFT JOIN latest_runs lr
          ON lr.historical_document_id = hd.id
        LEFT JOIN latest_ocr lo
          ON lo.source_pdf = hd.local_path
        LEFT JOIN page_text pt
          ON pt.source_pdf = hd.local_path
         AND (pt.file_hash IS hd.content_hash OR pt.file_hash = hd.content_hash)
        WHERE hd.state = 'NC'
    """
    params: list[Any] = []
    if company:
        query += " AND hd.company = ?"
        params.append(company)
    if family_key:
        query += " AND hd.family_key = ?"
        params.append(family_key)
    query += " ORDER BY hd.id DESC"

    rows = conn.execute(query, tuple(params)).fetchall()
    candidates: list[dict[str, Any]] = []
    route_counts: Counter[str] = Counter()
    lane_counts: Counter[str] = Counter()

    for row in rows:
        raw_text_chars = max(
            _safe_text_file_length(row["raw_text_path"]),
            int(row["page_artifact_text_chars"] or 0),
        )
        parser_profile = str(row["parser_profile"] or "unknown")
        outcome_quality = str(row["outcome_quality"] or "missing")
        stale_reasons = list(stale_by_document_id.get(int(row["historical_document_id"]), []) or [])
        bounded_page_count = 0
        if row["start_page"] and row["end_page"]:
            bounded_page_count = max(int(row["end_page"]) - int(row["start_page"]) + 1, 0)
        page_count = max(1, int(row["ocr_page_count"] or 0), bounded_page_count)
        route_reason, recommended_lane = _classify_ocr_route(
            raw_text_chars=raw_text_chars,
            outcome_quality=outcome_quality,
            parser_profile=parser_profile,
            page_count=page_count,
            title=str(row["title"] or ""),
            has_ocr_artifact=bool(row["ocr_backend"]),
            stale_reasons=stale_reasons,
        )
        if route_reason == "healthy_or_non_ocr_issue":
            continue

        priority = 0
        if route_reason == "no_usable_text_unknown_profile":
            priority = 0
        elif route_reason == "no_usable_text":
            priority = 1
        elif route_reason == "weak_without_ocr":
            priority = 2
        elif route_reason == "weak_layout_sensitive":
            priority = 3
        elif route_reason == "stale_artifacts":
            priority = 4
        else:
            priority = 5

        route_counts[route_reason] += 1
        lane_counts[recommended_lane] += 1
        candidates.append(
            {
                "historical_document_id": int(row["historical_document_id"]),
                "family_key": row["family_key"],
                "company": row["company"],
                "title": row["title"],
                "parser_profile": parser_profile,
                "outcome_quality": outcome_quality,
                "charge_count": int(row["charge_count"] or 0),
                "raw_text_chars": raw_text_chars,
                "ocr_backend": row["ocr_backend"],
                "ocr_status": row["ocr_status"],
                "ocr_confidence": row["ocr_confidence"],
                "page_count": page_count,
                "route_reason": route_reason,
                "recommended_lane": recommended_lane,
                "stale_reasons": stale_reasons,
                "_priority": priority,
            }
        )

    candidates.sort(
        key=lambda row: (
            int(row["_priority"]),
            int(row["raw_text_chars"]),
            0 if row["ocr_backend"] is None else 1,
            int(row["historical_document_id"]),
        )
    )

    trimmed = [{key: value for key, value in row.items() if key != "_priority"} for row in candidates[:limit]]
    return {
        "candidate_count": len(candidates),
        "route_reason_summary": [
            {"route_reason": name, "count": count}
            for name, count in route_counts.most_common(10)
        ],
        "recommended_lane_summary": [
            {"recommended_lane": name, "count": count}
            for name, count in lane_counts.most_common(10)
        ],
        "rows": trimmed,
    }
