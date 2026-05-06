from __future__ import annotations

from typing import Any

from duke_rates.db.reprocess import latest_processing_run_for_document
from duke_rates.db.repository import Repository


def _valid_text(value: Any) -> bool:
    if value is None:
        return False
    normalized = str(value).strip()
    if not normalized:
        return False
    return normalized.lower() not in {"none", "null"}


def _lookup_discovery_linkage(
    conn,
    *,
    local_path: str | None,
    content_hash: str | None,
) -> str:
    path_match = False
    hash_match = False
    if _valid_text(local_path):
        path_match = conn.execute(
            """
            SELECT 1
            FROM ncuc_discovery_records
            WHERE local_path = ?
            LIMIT 1
            """,
            (str(local_path),),
        ).fetchone() is not None
    if _valid_text(content_hash):
        hash_match = conn.execute(
            """
            SELECT 1
            FROM ncuc_discovery_records
            WHERE content_hash = ?
            LIMIT 1
            """,
            (str(content_hash),),
        ).fetchone() is not None

    if path_match and hash_match:
        return "path+hash"
    if hash_match:
        return "hash_only"
    if path_match:
        return "path_only"
    return "missing"


def build_lineage_validation_report(
    repo: Repository,
    *,
    limit: int = 25,
    family_key: str | None = None,
) -> dict[str, Any]:
    blocker_issue_types = {
        "missing_tariff_family",
        "missing_effective_start",
        "missing_version_link",
        "not_processed",
        "linked_without_charges",
    }
    warning_issue_types = {
        "provisional_family",
        "version_provenance_gap",
        "missing_discovery_match",
        "path_only_discovery_link",
    }

    with repo._connect() as conn:
        query = """
            SELECT
                hd.id,
                hd.family_key,
                hd.company,
                hd.title,
                hd.effective_start,
                hd.local_path,
                hd.content_hash,
                tf.family_type,
                tf.schedule_code,
                tf.notes AS family_notes
            FROM historical_documents hd
            LEFT JOIN tariff_families tf
              ON tf.family_key = hd.family_key
            WHERE hd.state = 'NC'
        """
        params: list[Any] = []
        if family_key:
            query += " AND hd.family_key = ?"
            params.append(family_key)
        query += " ORDER BY hd.id DESC"
        docs = [dict(row) for row in conn.execute(query, tuple(params)).fetchall()]

        validated_rows: list[dict[str, Any]] = []
        summary = {
            "total_documents_count": len(docs),
            "blocking_issue_document_count": 0,
            "warning_only_document_count": 0,
            "clean_document_count": 0,
            "missing_tariff_family_count": 0,
            "provisional_family_count": 0,
            "missing_effective_start_count": 0,
            "missing_version_link_count": 0,
            "not_processed_count": 0,
            "linked_without_charges_count": 0,
            "version_provenance_gap_count": 0,
            "missing_discovery_match_count": 0,
            "path_only_discovery_link_count": 0,
            "extracted_with_charges_count": 0,
            "skipped_reference_count": 0,
        }

        for doc in docs:
            version_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS version_count,
                    SUM(
                        CASE
                            WHEN COALESCE(TRIM(CAST(docket_number AS TEXT)), '') = ''
                              OR LOWER(TRIM(CAST(docket_number AS TEXT))) IN ('none', 'null')
                              OR COALESCE(TRIM(CAST(order_date AS TEXT)), '') = ''
                              OR LOWER(TRIM(CAST(order_date AS TEXT))) IN ('none', 'null')
                              OR COALESCE(TRIM(CAST(leaf_no AS TEXT)), '') = ''
                              OR LOWER(TRIM(CAST(leaf_no AS TEXT))) IN ('none', 'null')
                              OR COALESCE(TRIM(CAST(source_pdf AS TEXT)), '') = ''
                              OR LOWER(TRIM(CAST(source_pdf AS TEXT))) IN ('none', 'null')
                              OR COALESCE(TRIM(CAST(docket_dir AS TEXT)), '') = ''
                              OR LOWER(TRIM(CAST(docket_dir AS TEXT))) IN ('none', 'null')
                            THEN 1 ELSE 0
                        END
                    ) AS provenance_gap_count
                FROM tariff_versions
                WHERE historical_document_id = ?
                """,
                (int(doc["id"]),),
            ).fetchone()
            version_count = int(version_row["version_count"] or 0)
            version_provenance_gap_count = int(version_row["provenance_gap_count"] or 0)

            charge_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM tariff_versions tv
                    JOIN tariff_charges tc
                      ON tc.version_id = tv.id
                    WHERE tv.historical_document_id = ?
                    """,
                    (int(doc["id"]),),
                ).fetchone()[0]
            )

            latest_run = latest_processing_run_for_document(
                conn,
                historical_document_id=int(doc["id"]),
            )
            latest_outcome_quality = str((latest_run or {}).get("outcome_quality") or "")
            discovery_linkage = _lookup_discovery_linkage(
                conn,
                local_path=doc.get("local_path"),
                content_hash=doc.get("content_hash"),
            )

            family_exists = doc.get("family_type") is not None
            provisional_family = bool(
                family_exists
                and str(doc.get("family_notes") or "").startswith("Provisional historical family")
            )

            issues: list[str] = []
            if not family_exists:
                issues.append("missing_tariff_family")
                summary["missing_tariff_family_count"] += 1
            elif provisional_family:
                issues.append("provisional_family")
                summary["provisional_family_count"] += 1

            if doc.get("effective_start") is None:
                issues.append("missing_effective_start")
                summary["missing_effective_start_count"] += 1

            if version_count == 0:
                issues.append("missing_version_link")
                summary["missing_version_link_count"] += 1
            else:
                if latest_run is None:
                    issues.append("not_processed")
                    summary["not_processed_count"] += 1
                elif latest_outcome_quality == "skipped":
                    summary["skipped_reference_count"] += 1
                elif charge_count == 0:
                    issues.append("linked_without_charges")
                    summary["linked_without_charges_count"] += 1

                if charge_count > 0:
                    summary["extracted_with_charges_count"] += 1

                if version_provenance_gap_count > 0:
                    issues.append("version_provenance_gap")
                    summary["version_provenance_gap_count"] += 1

            if discovery_linkage == "missing":
                issues.append("missing_discovery_match")
                summary["missing_discovery_match_count"] += 1
            elif discovery_linkage == "path_only":
                issues.append("path_only_discovery_link")
                summary["path_only_discovery_link_count"] += 1

            row = {
                "historical_document_id": int(doc["id"]),
                "family_key": doc.get("family_key"),
                "company": doc.get("company"),
                "title": doc.get("title"),
                "effective_start": doc.get("effective_start"),
                "family_exists": family_exists,
                "provisional_family": provisional_family,
                "family_type": doc.get("family_type"),
                "schedule_code": doc.get("schedule_code"),
                "version_count": version_count,
                "charge_count": charge_count,
                "latest_outcome_quality": latest_outcome_quality or None,
                "latest_parser_profile": (latest_run or {}).get("parser_profile"),
                "discovery_linkage": discovery_linkage,
                "blocking_issues": [issue for issue in issues if issue in blocker_issue_types],
                "warning_issues": [issue for issue in issues if issue in warning_issue_types],
            }
            validated_rows.append(row)

        issue_rows = [row for row in validated_rows if row["blocking_issues"] or row["warning_issues"]]
        issue_rows.sort(
            key=lambda row: (
                -len(row["blocking_issues"]),
                -len(row["warning_issues"]),
                row["effective_start"] is None,
                -(row["version_count"]),
                -int(row["historical_document_id"]),
            )
        )

        for row in validated_rows:
            if row["blocking_issues"]:
                summary["blocking_issue_document_count"] += 1
            elif row["warning_issues"]:
                summary["warning_only_document_count"] += 1
            else:
                summary["clean_document_count"] += 1

    return {
        "summary": summary,
        "rows": issue_rows[:limit],
    }
