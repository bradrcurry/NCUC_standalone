from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from collections import Counter, defaultdict
from typing import Any


VALID_REVIEW_OUTCOMES = {"accepted", "corrected", "rejected", "needs_review"}


def _normalize_correction_key(key: str) -> str:
    return key.strip().lower().replace("-", "_").replace(" ", "_")


def _categorize_correction_key(key: str) -> str:
    normalized = _normalize_correction_key(key)
    if any(token in normalized for token in ("rate", "charge", "amount", "fixed", "energy", "demand", "kwh", "kw")):
        return "charge_value"
    if any(token in normalized for token in ("label", "title", "name")):
        return "label_or_title"
    if "date" in normalized:
        return "date"
    if any(token in normalized for token in ("schedule", "leaf", "family", "rider", "tariff")):
        return "tariff_identity"
    if any(token in normalized for token in ("season", "period", "tou", "tier", "block")):
        return "rate_structure"
    if any(token in normalized for token in ("parser", "status", "class", "company", "utility")):
        return "classification"
    if any(token in normalized for token in ("page", "source", "snippet", "span")):
        return "source_span"
    return "other"


def _extract_correction_fields(corrections: dict[str, Any]) -> list[str]:
    return sorted({_normalize_correction_key(key) for key in corrections})


def _extract_correction_categories(corrections: dict[str, Any]) -> list[str]:
    return sorted({_categorize_correction_key(key) for key in corrections})


def _derive_needs_review_root_cause(row: dict[str, Any]) -> str:
    status = str(row.get("status") or "").lower()
    profile = str(row.get("parser_profile") or "unknown").lower()
    review_source = str(row.get("review_source") or "unknown").lower()
    flags = json.loads(row.get("review_flags_json") or "[]")
    flag_set = {str(flag).lower() for flag in flags if str(flag).strip()}

    if "no_charges_extracted" in flag_set:
        return "no_charges_extracted"
    if "generic_fallback_selected" in flag_set:
        return "generic_fallback_selected"
    if "low_selector_confidence" in flag_set:
        return "low_selector_confidence"
    if "sparse_charge_set" in flag_set:
        return "sparse_charge_set"
    if "fallback_below_threshold" in flag_set:
        return "fallback_below_threshold"
    if status.startswith("skipped"):
        return "skipped_status"
    if review_source == "human":
        return "human_review_followup"
    if profile == "unknown":
        return "unknown_profile"
    if profile == "generic_residential":
        return "generic_residential_weak"
    return "other_needs_review"


def record_parse_review_outcome(
    conn: sqlite3.Connection,
    *,
    parse_attempt_id: int,
    outcome: str,
    review_source: str = "human",
    notes: dict[str, Any] | None = None,
    corrections: dict[str, Any] | None = None,
) -> int:
    """Attach a manual or rule-based review outcome to a parse attempt."""
    normalized_outcome = outcome.strip().lower()
    if normalized_outcome not in VALID_REVIEW_OUTCOMES:
        allowed = ", ".join(sorted(VALID_REVIEW_OUTCOMES))
        raise ValueError(f"Unsupported review outcome '{outcome}'. Expected one of: {allowed}")

    attempt = conn.execute(
        """
        SELECT id, source_pdf, docket_dir, page_start, page_end, parser_stage,
               parser_profile, utility
        FROM parse_attempt_logs
        WHERE id = ?
        """,
        (parse_attempt_id,),
    ).fetchone()
    if not attempt:
        raise ValueError(f"Parse attempt {parse_attempt_id} not found.")

    notes_payload = dict(notes or {})
    corrections_payload = dict(corrections or {})
    if corrections_payload:
        notes_payload.setdefault("correction_fields", _extract_correction_fields(corrections_payload))
        notes_payload.setdefault("correction_categories", _extract_correction_categories(corrections_payload))
    correction_count = int(notes_payload.get("correction_count", len(corrections_payload)))

    cur = conn.execute(
        """
        INSERT INTO parse_review_outcomes (
            parse_attempt_id, source_pdf, docket_dir, page_start, page_end,
            parser_stage, parser_profile, utility, review_source, outcome,
            correction_count, notes_json, corrections_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            parse_attempt_id,
            attempt["source_pdf"],
            attempt["docket_dir"],
            attempt["page_start"],
            attempt["page_end"],
            attempt["parser_stage"],
            attempt["parser_profile"],
            attempt["utility"],
            review_source,
            normalized_outcome,
            correction_count,
            json.dumps(notes_payload, sort_keys=True),
            json.dumps(corrections_payload, sort_keys=True),
            datetime.now(UTC).isoformat(),
        ),
    )
    return int(cur.lastrowid)


def _load_latest_parse_review_rows(
    conn: sqlite3.Connection,
    *,
    needs_review_only: bool = False,
) -> list[sqlite3.Row]:
    query = """
        WITH latest_attempts AS (
            SELECT pal.*
            FROM parse_attempt_logs pal
            JOIN (
                SELECT
                    source_pdf,
                    COALESCE(page_start, -1) AS page_start_key,
                    COALESCE(page_end, -1) AS page_end_key,
                    parser_stage,
                    MAX(id) AS max_id
                FROM parse_attempt_logs
                GROUP BY source_pdf, COALESCE(page_start, -1), COALESCE(page_end, -1), parser_stage
            ) latest_attempt
              ON latest_attempt.max_id = pal.id
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
            pal.id AS parse_attempt_id,
            pal.source_pdf,
            pal.docket_dir,
            pal.page_start,
            pal.page_end,
            pal.parser_stage,
            pal.parser_profile,
            pal.status,
            pal.confidence,
            pal.utility,
            pal.charge_count,
            pal.review_flags_json,
            pal.metadata_json,
            lr.id AS review_outcome_id,
            lr.review_source,
            lr.outcome,
            lr.correction_count,
            lr.notes_json,
            lr.corrections_json,
            lr.created_at AS reviewed_at
        FROM latest_attempts pal
        JOIN latest_reviews lr
          ON lr.parse_attempt_id = pal.id
    """
    if needs_review_only:
        query += " WHERE lr.outcome = 'needs_review'"
    query += " ORDER BY pal.id"
    return list(conn.execute(query).fetchall())


def _resolve_historical_review_lineage(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
) -> list[dict[str, Any]]:
    historical_ids: set[int] = set()
    parsed_metadata: dict[int, dict[str, Any]] = {}
    for row in rows:
        metadata = json.loads(row["metadata_json"] or "{}")
        parsed_metadata[int(row["parse_attempt_id"])] = metadata
        historical_id = metadata.get("historical_document_id")
        if isinstance(historical_id, int):
            historical_ids.add(historical_id)
        elif isinstance(historical_id, str) and historical_id.isdigit():
            historical_ids.add(int(historical_id))

    historical_docs: dict[int, sqlite3.Row] = {}
    if historical_ids:
        placeholders = ", ".join("?" for _ in sorted(historical_ids))
        query = f"""
            SELECT id, family_key, company, state
            FROM historical_documents
            WHERE id IN ({placeholders})
        """
        for historical_row in conn.execute(query, tuple(sorted(historical_ids))).fetchall():
            historical_docs[int(historical_row["id"])] = historical_row

    resolved_rows: list[dict[str, Any]] = []
    for row in rows:
        parse_attempt_id = int(row["parse_attempt_id"])
        metadata = dict(parsed_metadata.get(parse_attempt_id) or {})
        historical_id = metadata.get("historical_document_id")
        resolved_historical_id: int | None = None
        if isinstance(historical_id, int):
            resolved_historical_id = historical_id
        elif isinstance(historical_id, str) and historical_id.isdigit():
            resolved_historical_id = int(historical_id)
        if resolved_historical_id is not None:
            historical_doc = historical_docs.get(resolved_historical_id)
            if historical_doc is None:
                continue
            metadata["historical_document_id"] = resolved_historical_id
            metadata["family_key"] = historical_doc["family_key"]
            if historical_doc["company"]:
                metadata["company"] = historical_doc["company"]
            if historical_doc["state"]:
                metadata["state"] = historical_doc["state"]
        resolved_row = dict(row)
        resolved_row["metadata_json"] = json.dumps(metadata, sort_keys=True)
        resolved_rows.append(resolved_row)
    return resolved_rows


def _collapse_latest_operational_review_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        metadata = json.loads(row["metadata_json"] or "{}")
        historical_id = metadata.get("historical_document_id")
        parser_stage = str(row.get("parser_stage") or "")
        if isinstance(historical_id, int):
            key = ("historical", historical_id, parser_stage)
        elif isinstance(historical_id, str) and historical_id.isdigit():
            key = ("historical", int(historical_id), parser_stage)
        else:
            key = (
                "source",
                str(row.get("source_pdf") or ""),
                int(row.get("page_start") or -1),
                int(row.get("page_end") or -1),
                parser_stage,
            )
        existing = latest_by_key.get(key)
        if existing is None or int(row["parse_attempt_id"]) > int(existing["parse_attempt_id"]):
            latest_by_key[key] = row
    return list(latest_by_key.values())


def list_parse_review_queue(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    family_key: str | None = None,
    parser_profile: str | None = None,
    source_pdf: str | None = None,
) -> list[dict[str, Any]]:
    """Return parse attempts whose latest review outcome still needs review."""
    resolved_rows = _collapse_latest_operational_review_rows(
        _resolve_historical_review_lineage(
            conn,
            _load_latest_parse_review_rows(conn),
        )
    )
    filtered_rows: list[dict[str, Any]] = []
    for row in resolved_rows:
        if str(row.get("outcome") or "") != "needs_review":
            continue
        metadata = json.loads(row["metadata_json"] or "{}")
        if family_key and str(metadata.get("family_key") or "") != family_key:
            continue
        if parser_profile and str(row["parser_profile"] or "") != parser_profile:
            continue
        if source_pdf and str(row["source_pdf"] or "") != source_pdf:
            continue
        filtered_rows.append(row)
    filtered_rows.sort(key=lambda item: (float(item["confidence"] or 0.0), -int(item["parse_attempt_id"])))
    return filtered_rows[:limit]


def reconcile_skipped_rule_reviews(
    conn: sqlite3.Connection,
    *,
    limit: int = 0,
) -> dict[str, Any]:
    """Promote stale rule-based skipped parse attempts from needs_review to accepted."""
    query = """
        WITH latest_attempts AS (
            SELECT pal.*
            FROM parse_attempt_logs pal
            JOIN (
                SELECT
                    source_pdf,
                    COALESCE(page_start, -1) AS page_start_key,
                    COALESCE(page_end, -1) AS page_end_key,
                    parser_stage,
                    MAX(id) AS max_id
                FROM parse_attempt_logs
                GROUP BY source_pdf, COALESCE(page_start, -1), COALESCE(page_end, -1), parser_stage
            ) latest_attempt
              ON latest_attempt.max_id = pal.id
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
            pal.id AS parse_attempt_id,
            pal.source_pdf,
            pal.docket_dir,
            pal.page_start,
            pal.page_end,
            pal.parser_stage,
            pal.parser_profile,
            pal.utility,
            pal.status,
            pal.metadata_json,
            lr.id AS review_outcome_id,
            lr.notes_json
        FROM latest_attempts pal
        JOIN latest_reviews lr
          ON lr.parse_attempt_id = pal.id
        WHERE lr.outcome = 'needs_review'
          AND lr.review_source = 'rule'
          AND pal.status LIKE 'skipped\\_%' ESCAPE '\\'
        ORDER BY pal.id
    """
    params: tuple[Any, ...] = ()
    if limit > 0:
        query += " LIMIT ?"
        params = (limit,)
    rows = conn.execute(query, params).fetchall()

    inserted = 0
    parse_attempt_ids: list[int] = []
    for row in rows:
        notes = json.loads(row["notes_json"] or "{}")
        notes["reconciled_from_review_outcome_id"] = int(row["review_outcome_id"])
        notes["reconciled_reason"] = "skipped_status_now_accepted"
        notes["status"] = row["status"]
        review_id = record_parse_review_outcome(
            conn,
            parse_attempt_id=int(row["parse_attempt_id"]),
            outcome="accepted",
            review_source="rule",
            notes=notes,
        )
        if review_id:
            inserted += 1
            parse_attempt_ids.append(int(row["parse_attempt_id"]))

    return {
        "reconciled": inserted,
        "parse_attempt_ids": parse_attempt_ids,
    }


def build_parse_review_summary(
    conn: sqlite3.Connection,
    *,
    top_n: int = 10,
) -> dict[str, Any]:
    """Summarize latest parse-review outcomes to guide parser improvement work."""
    rows = _collapse_latest_operational_review_rows(
        _resolve_historical_review_lineage(conn, _load_latest_parse_review_rows(conn))
    )

    outcome_counts: Counter[str] = Counter()
    review_source_counts: Counter[str] = Counter()
    correction_category_counts: Counter[str] = Counter()
    correction_field_counts: Counter[str] = Counter()
    root_cause_counts: Counter[str] = Counter()
    parser_profiles: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "parser_profile": "",
        "attempt_count": 0,
        "needs_review": 0,
        "accepted": 0,
        "corrected": 0,
        "rejected": 0,
        "human_reviewed": 0,
        "correction_count": 0,
        "correction_categories": Counter(),
        "root_causes": Counter(),
    })
    families: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "family_key": "",
        "company": None,
        "attempt_count": 0,
        "needs_review": 0,
        "accepted": 0,
        "corrected": 0,
        "rejected": 0,
        "human_reviewed": 0,
        "correction_count": 0,
        "correction_categories": Counter(),
        "root_causes": Counter(),
    })

    for row in rows:
        metadata = json.loads(row["metadata_json"] or "{}")
        notes = json.loads(row["notes_json"] or "{}")
        corrections = json.loads(row["corrections_json"] or "{}")
        family_key = str(metadata.get("family_key") or "unknown")
        company = metadata.get("company")
        profile = str(row["parser_profile"] or "unknown")
        outcome = str(row["outcome"] or "needs_review")
        review_source = str(row["review_source"] or "unknown")
        correction_count = int(row["correction_count"] or 0)
        correction_categories = list(notes.get("correction_categories") or _extract_correction_categories(corrections))
        correction_fields = list(notes.get("correction_fields") or _extract_correction_fields(corrections))
        root_cause = _derive_needs_review_root_cause(row) if outcome == "needs_review" else None

        outcome_counts[outcome] += 1
        review_source_counts[review_source] += 1
        correction_category_counts.update(correction_categories)
        correction_field_counts.update(correction_fields)
        if root_cause:
            root_cause_counts[root_cause] += 1

        profile_bucket = parser_profiles[profile]
        profile_bucket["parser_profile"] = profile
        profile_bucket["attempt_count"] += 1
        profile_bucket[outcome] += 1
        profile_bucket["correction_count"] += correction_count
        if review_source == "human":
            profile_bucket["human_reviewed"] += 1
        profile_bucket["correction_categories"].update(correction_categories)
        if root_cause:
            profile_bucket["root_causes"].update([root_cause])

        family_bucket = families[family_key]
        family_bucket["family_key"] = family_key
        family_bucket["company"] = company
        family_bucket["attempt_count"] += 1
        family_bucket[outcome] += 1
        family_bucket["correction_count"] += correction_count
        if review_source == "human":
            family_bucket["human_reviewed"] += 1
        family_bucket["correction_categories"].update(correction_categories)
        if root_cause:
            family_bucket["root_causes"].update([root_cause])

    def _finalize_bucket(item: dict[str, Any]) -> dict[str, Any]:
        finalized = dict(item)
        counter = finalized.pop("correction_categories", Counter())
        finalized["top_correction_categories"] = [
            {"category": category, "count": count}
            for category, count in counter.most_common(3)
        ]
        root_cause_counter = finalized.pop("root_causes", Counter())
        finalized["top_root_causes"] = [
            {"root_cause": root_cause, "count": count}
            for root_cause, count in root_cause_counter.most_common(3)
        ]
        return finalized

    def _sort_key(item: dict[str, Any]) -> tuple[int, int, int, str]:
        return (
            int(item.get("corrected", 0)) + int(item.get("rejected", 0)) + int(item.get("needs_review", 0)),
            int(item.get("human_reviewed", 0)),
            int(item.get("correction_count", 0)),
            str(item.get("parser_profile") or item.get("family_key") or ""),
        )

    by_profile = [_finalize_bucket(item) for item in sorted(parser_profiles.values(), key=_sort_key, reverse=True)[:top_n]]
    by_family = [_finalize_bucket(item) for item in sorted(families.values(), key=_sort_key, reverse=True)[:top_n]]

    return {
        "summary": {
            "reviewed_attempt_count": len(rows),
            "outstanding_needs_review": outcome_counts.get("needs_review", 0),
            "accepted_count": outcome_counts.get("accepted", 0),
            "corrected_count": outcome_counts.get("corrected", 0),
            "rejected_count": outcome_counts.get("rejected", 0),
            "human_review_count": review_source_counts.get("human", 0),
            "rule_review_count": review_source_counts.get("rule", 0),
            "total_corrections_applied": sum(int(row["correction_count"] or 0) for row in rows),
        },
        "by_outcome": dict(sorted(outcome_counts.items())),
        "by_review_source": dict(sorted(review_source_counts.items())),
        "top_correction_categories": [
            {"category": category, "count": count}
            for category, count in correction_category_counts.most_common(top_n)
        ],
        "top_correction_fields": [
            {"field": field, "count": count}
            for field, count in correction_field_counts.most_common(top_n)
        ],
        "top_root_causes": [
            {"root_cause": root_cause, "count": count}
            for root_cause, count in root_cause_counts.most_common(top_n)
        ],
        "top_profiles": by_profile,
        "top_families": by_family,
    }
