from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from duke_rates.db.repository import Repository
from duke_rates.historical.ncuc.metadata import (
    extract_leaf_nos,
    extract_rider_codes,
    extract_schedule_codes,
)
from duke_rates.models.ncuc import NcucDiscoveryRecord
from duke_rates.utils.duke_company import normalize_duke_company


STRANDED_RECORD_SQL = """
SELECT
    ndr.*,
    nsa.extracted_leaf_nos_json,
    nsa.extracted_schedule_titles_json
FROM ncuc_discovery_records ndr
JOIN ncuc_span_artifacts nsa
  ON nsa.discovery_record_id = ndr.id
WHERE ndr.family_keys_json = '[]'
  AND (
    nsa.extracted_leaf_nos_json != '[]'
    OR nsa.extracted_schedule_titles_json != '[]'
  )
ORDER BY ndr.id, nsa.span_index
"""

GENERIC_TITLE_TOKENS = {
    "AND",
    "FOR",
    "THE",
    "WITH",
    "FROM",
    "THAT",
    "THIS",
    "SERVICE",
    "SERVICES",
    "SCHEDULE",
    "SCHEDULES",
    "RIDER",
    "RIDERS",
    "RATE",
    "RATES",
    "TARIFF",
    "TARIFFS",
    "ENERGY",
    "DUKE",
}


@dataclass
class FamilyCandidate:
    family_key: str
    company: str
    schedule_code: str | None
    leaf_nos: set[str]
    title_tokens: set[str]


def _normalize_code(code: str | None) -> str | None:
    if not code:
        return None
    normalized = re.sub(r"[^A-Z0-9]+", "", str(code).upper())
    if not normalized:
        return None
    return normalized.lstrip("0") or normalized


def _tokenize(text: str | None) -> set[str]:
    return {
        token
        for token in re.findall(r"[A-Z0-9]+", (text or "").upper())
        if len(token) >= 3 and token not in GENERIC_TITLE_TOKENS
    }


def _load_family_candidates(repo: Repository) -> list[FamilyCandidate]:
    leafs_by_family: dict[str, set[str]] = {}
    with repo._connect() as conn:
        rows = conn.execute(
            """
            SELECT family_key, leaf_no
            FROM historical_documents
            WHERE state = 'NC'
              AND company IN ('progress', 'carolinas')
              AND COALESCE(leaf_no, '') <> ''
            """
        ).fetchall()
        for row in rows:
            leafs_by_family.setdefault(row["family_key"], set()).add(str(row["leaf_no"]).lstrip("0"))

    candidates: list[FamilyCandidate] = []
    for family in repo.list_tariff_families(state="NC"):
        family_leafs = set(leafs_by_family.get(family.family_key, set()))
        family_key_leaf = re.search(r"leaf-(\d{1,4})$", family.family_key.lower())
        if family_key_leaf:
            family_leafs.add(family_key_leaf.group(1).lstrip("0"))

        title_tokens = _tokenize(family.title)
        for alias in family.aliases:
            title_tokens.update(_tokenize(alias))
        if family.schedule_code:
            title_tokens.add(_normalize_code(family.schedule_code) or "")

        candidates.append(
            FamilyCandidate(
                family_key=family.family_key,
                company=family.company.lower(),
                schedule_code=_normalize_code(family.schedule_code),
                leaf_nos={leaf for leaf in family_leafs if leaf},
                title_tokens={token for token in title_tokens if token},
            )
        )
    return candidates


def _load_stranded_records(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    for row in conn.execute(STRANDED_RECORD_SQL):
        record_id = int(row["id"])
        payload = grouped.setdefault(
            record_id,
            {
                "row": dict(row),
                "leaf_nos": set(),
                "schedule_titles": set(),
            },
        )
        payload["leaf_nos"].update(
            (value or "").lstrip("0")
            for value in json.loads(row["extracted_leaf_nos_json"] or "[]")
            if value
        )
        payload["schedule_titles"].update(
            value.strip()
            for value in json.loads(row["extracted_schedule_titles_json"] or "[]")
            if value and value.strip()
        )
    return grouped


def _infer_company(row: dict[str, Any]) -> str | None:
    utility_text = str(row.get("utility") or "")
    fallback = "progress"
    if "carolina" in utility_text.lower() and "progress" not in utility_text.lower():
        fallback = "carolinas"

    probe = " ".join(
        str(value or "")
        for value in (
            row.get("utility"),
            row.get("filing_title"),
            row.get("page_title"),
            row.get("local_path"),
            row.get("docket_number"),
        )
    )
    return normalize_duke_company(probe, fallback=fallback, state="NC")


def _score_candidates(
    *,
    row: dict[str, Any],
    extracted_leafs: set[str],
    schedule_titles: set[str],
    family_candidates: list[FamilyCandidate],
) -> list[dict[str, Any]]:
    inferred_company = _infer_company(row)
    extracted_codes: set[str] = set()
    extracted_riders: set[str] = set()
    title_tokens: set[str] = set()

    for title in schedule_titles:
        extracted_codes.update(
            code for code in (_normalize_code(item) for item in extract_schedule_codes(title)) if code
        )
        extracted_riders.update(
            code for code in (_normalize_code(item) for item in extract_rider_codes(title)) if code
        )
        title_tokens.update(_tokenize(title))

    title_tokens.update(_tokenize(row.get("filing_title")))
    for leaf in extract_leaf_nos(str(row.get("filing_title") or "")):
        extracted_leafs.add(leaf.lstrip("0"))

    matches: list[dict[str, Any]] = []
    code_pool = extracted_codes.union(extracted_riders)
    for family in family_candidates:
        if inferred_company and family.company != inferred_company:
            continue

        matched_leafs = sorted(family.leaf_nos.intersection(extracted_leafs))
        matched_code = family.schedule_code if family.schedule_code and family.schedule_code in code_pool else None
        shared_tokens = sorted(family.title_tokens.intersection(title_tokens))

        score = 0
        reasons: list[str] = []
        if matched_leafs:
            score += 100
            reasons.append(f"leaf:{','.join(matched_leafs[:3])}")
        if matched_code:
            score += 80
            reasons.append(f"code:{matched_code}")
        if len(shared_tokens) >= 2:
            score += min(20, len(shared_tokens) * 4)
            reasons.append(f"title:{','.join(shared_tokens[:4])}")

        if score < 80:
            continue

        matches.append(
            {
                "family_key": family.family_key,
                "score": score,
                "reasons": reasons,
            }
        )

    matches.sort(key=lambda item: (-int(item["score"]), str(item["family_key"])))
    return matches


def _schedule_codes_from_titles(schedule_titles: set[str]) -> list[str]:
    return sorted(
        {
            code
            for title in schedule_titles
            for code in (_normalize_code(item) for item in extract_schedule_codes(title))
            if code
        }
    )


def _row_to_discovery_record(
    row: dict[str, Any],
    *,
    family_keys: list[str],
    leaf_nos: list[str],
    schedule_codes: list[str],
    provenance_note: str,
) -> NcucDiscoveryRecord:
    return NcucDiscoveryRecord(
        id=int(row["id"]),
        docket_number=row["docket_number"],
        sub_number=row["sub_number"],
        utility=row["utility"],
        filing_title=row["filing_title"],
        filing_date=row["filing_date"],
        proceeding_type=row["proceeding_type"],
        filing_classification=row["filing_classification"],
        exhibit_label=row["exhibit_label"],
        referenced_schedule_codes=schedule_codes,
        referenced_rider_codes=json.loads(row["referenced_rider_codes_json"] or "[]"),
        referenced_leaf_nos=leaf_nos,
        family_keys=family_keys,
        discovered_url=row["discovered_url"],
        viewer_url=row["viewer_url"],
        attachment_url=row["attachment_url"],
        download_url=row["download_url"],
        acquisition_method=row["acquisition_method"],
        fetch_status=row["fetch_status"],
        local_path=row["local_path"],
        content_hash=row["content_hash"],
        content_type=row["content_type"],
        file_size_bytes=row["file_size_bytes"],
        provenance_notes=json.loads(row["provenance_notes_json"] or "[]") + [provenance_note],
        search_query=row["search_query"],
        page_title=row["page_title"],
        doc_quality_tier=row["doc_quality_tier"],
        search_confidence_score=row["search_confidence_score"],
        search_ideality=row["search_ideality"],
        error_detail=row["error_detail"],
        metadata_json=row["metadata_json"],
    )


def suggest_family_links(
    repo: Repository,
    *,
    limit: int | None = 25,
    record_id: int | None = None,
) -> list[dict[str, Any]]:
    family_candidates = _load_family_candidates(repo)
    with repo._connect() as conn:
        stranded = _load_stranded_records(conn)

    suggestions: list[dict[str, Any]] = []
    for candidate_record_id in sorted(stranded):
        if record_id is not None and candidate_record_id != record_id:
            continue
        payload = stranded[candidate_record_id]
        row = payload["row"]
        extracted_leafs = set(payload["leaf_nos"])
        schedule_titles = set(payload["schedule_titles"])
        matches = _score_candidates(
            row=row,
            extracted_leafs=extracted_leafs,
            schedule_titles=schedule_titles,
            family_candidates=family_candidates,
        )
        if not matches:
            continue
        schedule_codes = _schedule_codes_from_titles(schedule_titles)
        leaf_nos = sorted(value for value in extracted_leafs if value)
        suggestions.append(
            {
                "discovery_record_id": candidate_record_id,
                "docket_number": row["docket_number"],
                "utility": row["utility"],
                "filing_title": row["filing_title"],
                "leaf_nos": leaf_nos,
                "schedule_codes": schedule_codes,
                "family_keys": [match["family_key"] for match in matches],
                "matches": matches,
                "record": _row_to_discovery_record(
                    row,
                    family_keys=[match["family_key"] for match in matches],
                    leaf_nos=leaf_nos,
                    schedule_codes=schedule_codes,
                    provenance_note="family_keys_backfilled_from_span_artifacts",
                ),
            }
        )
        if limit is not None and len(suggestions) >= limit:
            break
    return suggestions


def apply_family_link_suggestions(
    repo: Repository,
    suggestions: list[dict[str, Any]],
) -> int:
    updated = 0
    for item in suggestions:
        repo.upsert_ncuc_discovery_record(item["record"])
        updated += 1
    return updated


def build_lineage_gap_report(
    repo: Repository,
    *,
    limit: int = 25,
) -> dict[str, Any]:
    with repo._connect() as conn:
        unlinked_discovery_records_count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM ncuc_discovery_records
                WHERE family_keys_json = '[]'
                """
            ).fetchone()[0]
        )
        historical_missing_effective_start_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, family_key, company, title, local_path
                FROM historical_documents
                WHERE state = 'NC'
                  AND local_path IS NOT NULL
                  AND effective_start IS NULL
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]
        historical_missing_effective_start_count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM historical_documents
                WHERE state = 'NC'
                  AND local_path IS NOT NULL
                  AND effective_start IS NULL
                """
            ).fetchone()[0]
        )
        historical_missing_version_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT hd.id, hd.family_key, hd.company, hd.effective_start, hd.title, hd.local_path
                FROM historical_documents hd
                WHERE hd.state = 'NC'
                  AND hd.local_path IS NOT NULL
                  AND hd.effective_start IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM tariff_versions tv WHERE tv.historical_document_id = hd.id
                  )
                ORDER BY hd.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]
        historical_missing_version_count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM historical_documents hd
                WHERE hd.state = 'NC'
                  AND hd.local_path IS NOT NULL
                  AND hd.effective_start IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM tariff_versions tv WHERE tv.historical_document_id = hd.id
                  )
                """
            ).fetchone()[0]
        )
        versions_missing_historical_document_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT tv.id, tv.family_key, tf.company, tv.effective_start, tv.source_type, tv.notes
                FROM tariff_versions tv
                JOIN tariff_families tf
                  ON tf.family_key = tv.family_key
                WHERE tf.state = 'NC'
                  AND tv.historical_document_id IS NULL
                ORDER BY tv.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]
        versions_missing_historical_document_count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM tariff_versions tv
                JOIN tariff_families tf
                  ON tf.family_key = tv.family_key
                WHERE tf.state = 'NC'
                  AND tv.historical_document_id IS NULL
                """
            ).fetchone()[0]
        )
        families_without_charges_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT
                    tf.family_key,
                    tf.company,
                    tf.family_type,
                    COUNT(DISTINCT tv.id) AS version_count,
                    COUNT(DISTINCT hd.id) AS historical_document_count
                FROM tariff_families tf
                LEFT JOIN tariff_versions tv
                  ON tv.family_key = tf.family_key
                LEFT JOIN tariff_charges tc
                  ON tc.version_id = tv.id
                LEFT JOIN historical_documents hd
                  ON hd.family_key = tf.family_key
                WHERE tf.state = 'NC'
                  AND COALESCE(tf.notes, '') NOT LIKE 'Provisional historical family%'
                GROUP BY tf.family_key, tf.company, tf.family_type
                HAVING COUNT(tc.id) = 0
                ORDER BY historical_document_count DESC, version_count DESC, tf.family_key
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]
        families_without_charges_count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM (
                    SELECT tf.family_key
                    FROM tariff_families tf
                    LEFT JOIN tariff_versions tv
                      ON tv.family_key = tf.family_key
                    LEFT JOIN tariff_charges tc
                      ON tc.version_id = tv.id
                    WHERE tf.state = 'NC'
                      AND COALESCE(tf.notes, '') NOT LIKE 'Provisional historical family%'
                    GROUP BY tf.family_key
                    HAVING COUNT(tc.id) = 0
                ) gap_families
                """
            ).fetchone()[0]
        )

    all_suggestions = suggest_family_links(repo, limit=None)
    suggestions = all_suggestions[:limit]
    auto_matchable_discovery_records_count = len(all_suggestions)

    return {
        "summary": {
            "unlinked_discovery_records_count": unlinked_discovery_records_count,
            "auto_matchable_discovery_records_count": auto_matchable_discovery_records_count,
            "historical_missing_effective_start_count": historical_missing_effective_start_count,
            "historical_missing_version_count": historical_missing_version_count,
            "versions_missing_historical_document_id_count": versions_missing_historical_document_count,
            "families_without_charges_count": families_without_charges_count,
        },
        "auto_matchable_discovery_records": [
            {
                "discovery_record_id": item["discovery_record_id"],
                "docket_number": item["docket_number"],
                "utility": item["utility"],
                "filing_title": item["filing_title"],
                "leaf_nos": item["leaf_nos"],
                "schedule_codes": item["schedule_codes"],
                "top_match": item["matches"][0],
            }
            for item in suggestions
        ],
        "historical_missing_effective_start": historical_missing_effective_start_rows,
        "historical_missing_version_link": historical_missing_version_rows,
        "versions_missing_historical_document_id": versions_missing_historical_document_rows,
        "families_without_charges": families_without_charges_rows,
    }
