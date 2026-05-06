from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from duke_rates.analytics.nc_anomaly_audit import build_nc_anomaly_audit
from duke_rates.analytics.nc_document_gap_audit import build_nc_document_gap_audit
from duke_rates.analytics.tariff_completeness_audit import TariffCompletenessAuditService
from duke_rates.db.repository import Repository

_DEFAULT_OUTPUT_DIR = Path("docs/reports/nc_confidence_audit")
_DUAL_RATE_REGEX = re.compile(r"\b\d{1,4}\.\d{3,6}\s*/\s*\d{1,4}\.\d{3,6}\b")
_COMPARATIVE_RATE_REGEX = re.compile(
    r"(?i)\b(?:was|previously|prior|changed\s+from|from\s+\$?\d[\d,.]+\s+to)\b"
)
_INSERT_DELETE_REGEX = re.compile(
    r"(?i)\b(?:deleted text|inserted text|strike[- ]?through|strikethrough|tracked changes)\b"
)
_SUPERCESSION_CLUE_REGEX = re.compile(
    r"(?i)\b(?:supersedes|superceded|replaces|revised leaf no\.?)\b"
)


@dataclass(frozen=True)
class _FamilySignals:
    family_key: str
    utility: str
    title: str | None
    family_type: str
    schedule_code: str | None
    version_count: int
    versions_with_charges: int
    linked_historical_docs: int
    current_document_id: int | None
    earliest_effective_start: str | None
    latest_effective_start: str | None


def _connect(database_path: Path | None = None) -> sqlite3.Connection:
    path = Path(database_path or "data/db/duke_rates.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def build_nc_confidence_audit(
    database_path: Path | None = None,
) -> dict[str, object]:
    base_rows = _load_family_rows(database_path)
    anomaly_report = build_nc_anomaly_audit(database_path, include_only_flagged=True)
    gap_report = build_nc_document_gap_audit(database_path)
    anomaly_by_family = _group_anomalies_by_family(anomaly_report["rows"])
    gap_by_family = _group_gaps_by_family(gap_report["rows"])
    doc_signals = _load_document_signals(database_path)

    repo = Repository(Path(database_path or "data/db/duke_rates.db"))
    svc = TariffCompletenessAuditService(repo)

    rows: list[dict[str, object]] = []
    tier_counts: Counter[str] = Counter()
    recommended_action_counts: Counter[str] = Counter()

    for base in base_rows:
        temporal_map = svc.build_temporal_map(base.family_key)
        anomaly_group = anomaly_by_family.get(base.family_key, {})
        gap_group = gap_by_family.get(base.family_key, {})
        doc_group = doc_signals.get(base.family_key, {})

        row = _build_row(
            base=base,
            temporal_map=temporal_map,
            anomaly_group=anomaly_group,
            gap_group=gap_group,
            doc_group=doc_group,
        )
        rows.append(row)
        tier_counts[str(row["confidence_tier"])] += 1
        recommended_action_counts[str(row["recommended_action"])] += 1

    rows.sort(
        key=lambda item: (
            float(item["confidence_score"]),
            -int(item["version_count"]),
            str(item["family_key"]),
        )
    )

    return {
        "generated_at": date.today().isoformat(),
        "total_families": len(rows),
        "tier_counts": dict(sorted(tier_counts.items())),
        "recommended_action_counts": dict(sorted(recommended_action_counts.items())),
        "rows": rows,
    }


def export_nc_confidence_audit(
    output_dir: Path,
    *,
    database_path: Path | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_nc_confidence_audit(database_path)

    rows_csv = output_dir / "nc_confidence_audit_rows.csv"
    summary_json = output_dir / "nc_confidence_audit_summary.json"
    markdown_path = output_dir / "nc_confidence_audit.md"

    _write_csv(rows_csv, report["rows"])
    summary_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")

    return {
        "rows_csv": rows_csv,
        "summary_json": summary_json,
        "markdown": markdown_path,
    }


def _load_family_rows(database_path: Path | None) -> list[_FamilySignals]:
    conn = _connect(database_path)
    try:
        rows = conn.execute(
            """
            WITH family_summary AS (
                SELECT
                    tf.family_key,
                    tf.company,
                    tf.title,
                    tf.family_type,
                    tf.schedule_code,
                    tf.current_document_id,
                    COUNT(DISTINCT tv.id) AS version_count,
                    COUNT(DISTINCT CASE WHEN vcs.charge_count > 0 THEN tv.id END) AS versions_with_charges,
                    COUNT(DISTINCT tv.historical_document_id) AS linked_historical_docs,
                    MIN(tv.effective_start) AS earliest_effective_start,
                    MAX(tv.effective_start) AS latest_effective_start
                FROM tariff_families tf
                LEFT JOIN tariff_versions tv
                  ON tv.family_key = tf.family_key
                LEFT JOIN v_version_charge_summary vcs
                  ON vcs.version_id = tv.id
                WHERE tf.state = 'NC'
                  AND LOWER(tf.company) IN ('progress', 'carolinas')
                  AND tf.family_type IN ('rate_schedule', 'rider')
                GROUP BY
                    tf.family_key,
                    tf.company,
                    tf.title,
                    tf.family_type,
                    tf.schedule_code,
                    tf.current_document_id
            )
            SELECT *
            FROM family_summary
            ORDER BY company, family_key
            """
        ).fetchall()
    finally:
        conn.close()

    items: list[_FamilySignals] = []
    for row in rows:
        company = str(row["company"] or "").lower()
        items.append(
            _FamilySignals(
                family_key=str(row["family_key"]),
                utility="DEP" if company == "progress" else "DEC",
                title=row["title"],
                family_type=str(row["family_type"]),
                schedule_code=row["schedule_code"],
                version_count=int(row["version_count"] or 0),
                versions_with_charges=int(row["versions_with_charges"] or 0),
                linked_historical_docs=int(row["linked_historical_docs"] or 0),
                current_document_id=row["current_document_id"],
                earliest_effective_start=row["earliest_effective_start"],
                latest_effective_start=row["latest_effective_start"],
            )
        )
    return items


def _load_document_signals(
    database_path: Path | None,
) -> dict[str, dict[str, object]]:
    conn = _connect(database_path)
    try:
        rows = conn.execute(
            """
            WITH matched_fingerprints AS (
                SELECT
                    hd.id AS historical_document_id,
                    COALESCE(df.is_redline_candidate, 0) AS is_redline_candidate,
                    COALESCE(df.redline_confidence, 0.0) AS redline_confidence,
                    df.doc_quality_tier AS doc_quality_tier,
                    ROW_NUMBER() OVER (
                        PARTITION BY hd.id
                        ORDER BY
                            CASE
                                WHEN df.page_start IS hd.start_page AND df.page_end IS hd.end_page THEN 2
                                WHEN df.page_start IS NULL AND df.page_end IS NULL THEN 1
                                ELSE 0
                            END DESC,
                            df.id DESC
                    ) AS rn
                FROM historical_documents hd
                LEFT JOIN document_fingerprints df
                  ON df.source_pdf = hd.local_path
                 AND (
                    (df.page_start IS hd.start_page AND df.page_end IS hd.end_page)
                    OR (df.page_start IS NULL AND df.page_end IS NULL)
                 )
                WHERE hd.state = 'NC'
                  AND LOWER(hd.company) IN ('progress', 'carolinas')
            ),
            doc_runs AS (
                SELECT
                    historical_document_id,
                    parser_profile,
                    outcome_quality,
                    ROW_NUMBER() OVER (
                        PARTITION BY historical_document_id
                        ORDER BY id DESC
                    ) AS rn
                FROM historical_processing_runs
            )
            SELECT
                hd.family_key,
                COUNT(DISTINCT hd.id) AS historical_doc_count,
                SUM(CASE WHEN COALESCE(df.is_redline_candidate, 0) = 1 THEN 1 ELSE 0 END) AS redline_doc_count,
                MAX(COALESCE(df.redline_confidence, 0.0)) AS max_redline_confidence,
                SUM(CASE WHEN df.doc_quality_tier = 'T1' THEN 1 ELSE 0 END) AS t1_doc_count,
                SUM(CASE WHEN df.doc_quality_tier = 'T2' THEN 1 ELSE 0 END) AS t2_doc_count,
                SUM(CASE WHEN df.doc_quality_tier = 'T3' THEN 1 ELSE 0 END) AS t3_doc_count,
                SUM(CASE WHEN lr.outcome_quality = 'weak' THEN 1 ELSE 0 END) AS weak_parse_doc_count,
                SUM(CASE WHEN lr.parser_profile = 'generic_residential' THEN 1 ELSE 0 END) AS generic_profile_doc_count
            FROM historical_documents hd
            LEFT JOIN matched_fingerprints df
              ON df.historical_document_id = hd.id
             AND df.rn = 1
            LEFT JOIN doc_runs lr
              ON lr.historical_document_id = hd.id
             AND lr.rn = 1
            WHERE hd.state = 'NC'
              AND LOWER(hd.company) IN ('progress', 'carolinas')
            GROUP BY hd.family_key
            """
        ).fetchall()

        paired_redlines = conn.execute(
            """
            WITH matched_fingerprints AS (
                SELECT
                    hd.id AS historical_document_id,
                    hd.family_key,
                    hd.effective_start,
                    COALESCE(df.is_redline_candidate, 0) AS is_redline_candidate,
                    ROW_NUMBER() OVER (
                        PARTITION BY hd.id
                        ORDER BY
                            CASE
                                WHEN df.page_start IS hd.start_page AND df.page_end IS hd.end_page THEN 2
                                WHEN df.page_start IS NULL AND df.page_end IS NULL THEN 1
                                ELSE 0
                            END DESC,
                            df.id DESC
                    ) AS rn
                FROM historical_documents hd
                LEFT JOIN document_fingerprints df
                  ON df.source_pdf = hd.local_path
                 AND (
                    (df.page_start IS hd.start_page AND df.page_end IS hd.end_page)
                    OR (df.page_start IS NULL AND df.page_end IS NULL)
                 )
                WHERE hd.state = 'NC'
                  AND LOWER(hd.company) IN ('progress', 'carolinas')
            ),
            redline_docs AS (
                SELECT
                    family_key,
                    effective_start,
                    historical_document_id AS id
                FROM matched_fingerprints
                WHERE rn = 1
                  AND COALESCE(is_redline_candidate, 0) = 1
            )
            SELECT
                red.family_key,
                SUM(
                    CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM matched_fingerprints mf2
                            WHERE mf2.rn = 1
                              AND mf2.family_key = red.family_key
                              AND COALESCE(mf2.effective_start, '') = COALESCE(red.effective_start, '')
                              AND COALESCE(mf2.is_redline_candidate, 0) = 0
                        )
                        THEN 1 ELSE 0
                    END
                ) AS corroborated_redline_doc_count,
                SUM(
                    CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM matched_fingerprints mf2
                            WHERE mf2.rn = 1
                              AND mf2.family_key = red.family_key
                              AND COALESCE(mf2.effective_start, '') = COALESCE(red.effective_start, '')
                              AND COALESCE(mf2.is_redline_candidate, 0) = 0
                        )
                        THEN 0 ELSE 1
                    END
                ) AS unpaired_redline_doc_count
            FROM redline_docs red
            GROUP BY red.family_key
            """
        ).fetchall()

        redline_docs = conn.execute(
            """
            WITH matched_fingerprints AS (
                SELECT
                    hd.id AS historical_document_id,
                    COALESCE(df.is_redline_candidate, 0) AS is_redline_candidate,
                    ROW_NUMBER() OVER (
                        PARTITION BY hd.id
                        ORDER BY
                            CASE
                                WHEN df.page_start IS hd.start_page AND df.page_end IS hd.end_page THEN 2
                                WHEN df.page_start IS NULL AND df.page_end IS NULL THEN 1
                                ELSE 0
                            END DESC,
                            df.id DESC
                    ) AS rn
                FROM historical_documents hd
                LEFT JOIN document_fingerprints df
                  ON df.source_pdf = hd.local_path
                 AND (
                    (df.page_start IS hd.start_page AND df.page_end IS hd.end_page)
                    OR (df.page_start IS NULL AND df.page_end IS NULL)
                 )
                WHERE hd.state = 'NC'
                  AND LOWER(hd.company) IN ('progress', 'carolinas')
            )
            SELECT
                hd.id,
                hd.family_key,
                hd.effective_start,
                hd.raw_text_path,
                hd.revision_label,
                hd.supersedes_label,
                hd.local_path
            FROM historical_documents hd
            JOIN matched_fingerprints df
              ON df.historical_document_id = hd.id
             AND df.rn = 1
            WHERE hd.state = 'NC'
              AND LOWER(hd.company) IN ('progress', 'carolinas')
              AND COALESCE(df.is_redline_candidate, 0) = 1
            """
        ).fetchall()
    finally:
        conn.close()

    by_family: dict[str, dict[str, object]] = {}
    for row in rows:
        by_family[str(row["family_key"])] = {
            "historical_doc_count": int(row["historical_doc_count"] or 0),
            "redline_doc_count": int(row["redline_doc_count"] or 0),
            "max_redline_confidence": float(row["max_redline_confidence"] or 0.0),
            "t1_doc_count": int(row["t1_doc_count"] or 0),
            "t2_doc_count": int(row["t2_doc_count"] or 0),
            "t3_doc_count": int(row["t3_doc_count"] or 0),
            "weak_parse_doc_count": int(row["weak_parse_doc_count"] or 0),
            "generic_profile_doc_count": int(row["generic_profile_doc_count"] or 0),
            "corroborated_redline_doc_count": 0,
            "unpaired_redline_doc_count": 0,
        }

    for row in paired_redlines:
        bucket = by_family.setdefault(str(row["family_key"]), {})
        bucket["corroborated_redline_doc_count"] = int(row["corroborated_redline_doc_count"] or 0)
        bucket["unpaired_redline_doc_count"] = int(row["unpaired_redline_doc_count"] or 0)

    for row in redline_docs:
        family_key = str(row["family_key"])
        bucket = by_family.setdefault(family_key, {})
        clues = _extract_redline_clues(
            raw_text_path=row["raw_text_path"],
            revision_label=row["revision_label"],
            supersedes_label=row["supersedes_label"],
        )
        bucket["redline_clue_doc_count"] = int(bucket.get("redline_clue_doc_count", 0)) + (
            1 if clues["has_any_actionable_clue"] else 0
        )
        bucket["dual_rate_pair_doc_count"] = int(bucket.get("dual_rate_pair_doc_count", 0)) + (
            1 if clues["dual_rate_pair_count"] > 0 else 0
        )
        bucket["comparative_phrase_doc_count"] = int(bucket.get("comparative_phrase_doc_count", 0)) + (
            1 if clues["comparative_phrase_count"] > 0 else 0
        )
        bucket["insert_delete_marker_doc_count"] = int(bucket.get("insert_delete_marker_doc_count", 0)) + (
            1 if clues["insert_delete_marker_count"] > 0 else 0
        )
        bucket["supersession_clue_doc_count"] = int(bucket.get("supersession_clue_doc_count", 0)) + (
            1 if clues["supersession_clue_count"] > 0 else 0
        )
        bucket["max_dual_rate_pair_count"] = max(
            int(bucket.get("max_dual_rate_pair_count", 0)),
            int(clues["dual_rate_pair_count"]),
        )

    return by_family


def _extract_redline_clues(
    *,
    raw_text_path: str | None,
    revision_label: str | None,
    supersedes_label: str | None,
) -> dict[str, int | bool]:
    text = ""
    if raw_text_path:
        path = Path(str(raw_text_path))
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                text = ""

    dual_rate_pair_count = len(_DUAL_RATE_REGEX.findall(text))
    comparative_phrase_count = len(_COMPARATIVE_RATE_REGEX.findall(text))
    insert_delete_marker_count = len(_INSERT_DELETE_REGEX.findall(text))
    supersession_clue_count = len(_SUPERCESSION_CLUE_REGEX.findall(text))
    if revision_label:
        supersession_clue_count += 1
    if supersedes_label:
        supersession_clue_count += 1

    return {
        "dual_rate_pair_count": dual_rate_pair_count,
        "comparative_phrase_count": comparative_phrase_count,
        "insert_delete_marker_count": insert_delete_marker_count,
        "supersession_clue_count": supersession_clue_count,
        "has_any_actionable_clue": any(
            [
                dual_rate_pair_count > 0,
                comparative_phrase_count > 0,
                insert_delete_marker_count > 0,
                supersession_clue_count > 0,
            ]
        ),
    }


def _group_anomalies_by_family(rows: object) -> dict[str, dict[str, object]]:
    grouped: dict[str, dict[str, object]] = defaultdict(
        lambda: {"count": 0, "by_type": Counter(), "flagged_versions": set()}
    )
    for row in rows:  # type: ignore[assignment]
        family_key = str(row.get("family_key") or "")
        bucket = grouped[family_key]
        bucket["count"] += 1
        bucket["by_type"][str(row.get("anomaly_type") or "unknown")] += 1
        bucket["flagged_versions"].add(int(row.get("version_id") or 0))
    for bucket in grouped.values():
        bucket["flagged_version_count"] = len(bucket["flagged_versions"])
        bucket["anomaly_types"] = dict(sorted(bucket["by_type"].items()))
        del bucket["flagged_versions"]
        del bucket["by_type"]
    return grouped


def _group_gaps_by_family(rows: object) -> dict[str, dict[str, object]]:
    grouped: dict[str, dict[str, object]] = defaultdict(
        lambda: {"count": 0, "by_type": Counter(), "max_priority_score": 0}
    )
    for row in rows:  # type: ignore[assignment]
        family_key = str(row.get("family_key") or "")
        bucket = grouped[family_key]
        bucket["count"] += 1
        bucket["by_type"][str(row.get("gap_type") or "unknown")] += 1
        bucket["max_priority_score"] = max(
            int(bucket["max_priority_score"]),
            int(row.get("priority_score") or 0),
        )
    for bucket in grouped.values():
        bucket["gap_types"] = dict(sorted(bucket["by_type"].items()))
        del bucket["by_type"]
    return grouped


def _build_row(
    *,
    base: _FamilySignals,
    temporal_map,
    anomaly_group: dict[str, object],
    gap_group: dict[str, object],
    doc_group: dict[str, object],
) -> dict[str, object]:
    version_count = max(0, base.version_count)
    versions_with_charges = max(0, base.versions_with_charges)
    coverage_ratio = (
        float(versions_with_charges / version_count) if version_count > 0 else 0.0
    )

    timeline_status = str(temporal_map.timeline_status or "empty")
    timeline_gap_count = len(temporal_map.gaps)
    orphan_revision_count = len(temporal_map.orphaned_revisions)

    anomaly_count = int(anomaly_group.get("count", 0))
    flagged_version_count = int(anomaly_group.get("flagged_version_count", 0))
    gap_opportunity_count = int(gap_group.get("count", 0))
    max_gap_priority_score = int(gap_group.get("max_priority_score", 0))

    historical_doc_count = int(doc_group.get("historical_doc_count", 0))
    redline_doc_count = int(doc_group.get("redline_doc_count", 0))
    corroborated_redline_doc_count = int(doc_group.get("corroborated_redline_doc_count", 0))
    unpaired_redline_doc_count = int(doc_group.get("unpaired_redline_doc_count", 0))
    redline_clue_doc_count = int(doc_group.get("redline_clue_doc_count", 0))
    dual_rate_pair_doc_count = int(doc_group.get("dual_rate_pair_doc_count", 0))
    comparative_phrase_doc_count = int(doc_group.get("comparative_phrase_doc_count", 0))
    insert_delete_marker_doc_count = int(doc_group.get("insert_delete_marker_doc_count", 0))
    supersession_clue_doc_count = int(doc_group.get("supersession_clue_doc_count", 0))
    max_dual_rate_pair_count = int(doc_group.get("max_dual_rate_pair_count", 0))
    weak_parse_doc_count = int(doc_group.get("weak_parse_doc_count", 0))
    generic_profile_doc_count = int(doc_group.get("generic_profile_doc_count", 0))
    t1_doc_count = int(doc_group.get("t1_doc_count", 0))
    t2_doc_count = int(doc_group.get("t2_doc_count", 0))
    t3_doc_count = int(doc_group.get("t3_doc_count", 0))
    max_redline_confidence = round(float(doc_group.get("max_redline_confidence", 0.0)), 4)

    coverage_score = 35.0 * coverage_ratio

    timeline_base = {
        "complete": 25.0,
        "gaps_exist": 14.0,
        "undated": 8.0,
        "empty": 0.0,
    }.get(timeline_status, 4.0)
    timeline_penalty = min(15.0, (timeline_gap_count * 2.0) + orphan_revision_count)
    timeline_score = max(0.0, timeline_base - timeline_penalty)

    parse_score = max(
        0.0,
        20.0
        - min(
            20.0,
            (anomaly_count * 2.5)
            + weak_parse_doc_count
            + (generic_profile_doc_count * 0.5),
        ),
    )

    source_denominator = max(1, historical_doc_count)
    source_score = min(
        10.0,
        (
            (2.0 * t1_doc_count)
            + (1.5 * t2_doc_count)
            + (1.0 * t3_doc_count)
        )
        / (2.0 * source_denominator)
        * 10.0,
    )

    redline_score = 10.0
    if redline_doc_count > 0:
        redline_score = max(
            0.0,
            10.0
            - (unpaired_redline_doc_count * 2.0)
            + min(2.0, corroborated_redline_doc_count * 0.5)
            + min(2.0, redline_clue_doc_count * 0.25),
        )

    gap_penalty = min(12.0, (gap_opportunity_count * 1.5) + (max_gap_priority_score / 50.0))

    confidence_score = round(
        max(
            0.0,
            min(
                100.0,
                coverage_score + timeline_score + parse_score + source_score + redline_score - gap_penalty,
            ),
        ),
        1,
    )
    confidence_tier = _score_to_tier(confidence_score)
    recommended_action, rationale = _recommend_action(
        confidence_score=confidence_score,
        timeline_status=timeline_status,
        gap_opportunity_count=gap_opportunity_count,
        unpaired_redline_doc_count=unpaired_redline_doc_count,
        redline_clue_doc_count=redline_clue_doc_count,
        anomaly_count=anomaly_count,
        weak_parse_doc_count=weak_parse_doc_count,
        coverage_ratio=coverage_ratio,
    )

    return {
        "utility": base.utility,
        "family_key": base.family_key,
        "title": base.title,
        "family_type": base.family_type,
        "schedule_code": base.schedule_code,
        "version_count": version_count,
        "versions_with_charges": versions_with_charges,
        "coverage_pct": round(coverage_ratio * 100.0, 1),
        "linked_historical_docs": base.linked_historical_docs,
        "historical_doc_count": historical_doc_count,
        "earliest_effective_start": base.earliest_effective_start,
        "latest_effective_start": base.latest_effective_start,
        "timeline_status": timeline_status,
        "timeline_gap_count": timeline_gap_count,
        "orphan_revision_count": orphan_revision_count,
        "gap_opportunity_count": gap_opportunity_count,
        "gap_types": json.dumps(gap_group.get("gap_types", {}), sort_keys=True),
        "max_gap_priority_score": max_gap_priority_score,
        "anomaly_count": anomaly_count,
        "flagged_version_count": flagged_version_count,
        "anomaly_types": json.dumps(anomaly_group.get("anomaly_types", {}), sort_keys=True),
        "redline_doc_count": redline_doc_count,
        "corroborated_redline_doc_count": corroborated_redline_doc_count,
        "unpaired_redline_doc_count": unpaired_redline_doc_count,
        "redline_clue_doc_count": redline_clue_doc_count,
        "dual_rate_pair_doc_count": dual_rate_pair_doc_count,
        "comparative_phrase_doc_count": comparative_phrase_doc_count,
        "insert_delete_marker_doc_count": insert_delete_marker_doc_count,
        "supersession_clue_doc_count": supersession_clue_doc_count,
        "max_dual_rate_pair_count": max_dual_rate_pair_count,
        "max_redline_confidence": max_redline_confidence,
        "weak_parse_doc_count": weak_parse_doc_count,
        "generic_profile_doc_count": generic_profile_doc_count,
        "t1_doc_count": t1_doc_count,
        "t2_doc_count": t2_doc_count,
        "t3_doc_count": t3_doc_count,
        "confidence_score": confidence_score,
        "confidence_tier": confidence_tier,
        "recommended_action": recommended_action,
        "rationale": rationale,
    }


def _score_to_tier(score: float) -> str:
    if score >= 80.0:
        return "high"
    if score >= 60.0:
        return "medium"
    if score >= 40.0:
        return "low"
    return "weak"


def _recommend_action(
    *,
    confidence_score: float,
    timeline_status: str,
    gap_opportunity_count: int,
    unpaired_redline_doc_count: int,
    redline_clue_doc_count: int,
    anomaly_count: int,
    weak_parse_doc_count: int,
    coverage_ratio: float,
) -> tuple[str, str]:
    if (
        confidence_score >= 85.0
        and timeline_status == "complete"
        and anomaly_count == 0
        and weak_parse_doc_count == 0
        and unpaired_redline_doc_count == 0
        and coverage_ratio >= 0.9
    ):
        return (
            "likely_ok",
            "Signals are aligned strongly enough that any remaining gap heuristics look secondary rather than blocking.",
        )
    if gap_opportunity_count > 0 or timeline_status in {"gaps_exist", "undated", "empty"}:
        return (
            "search_for_missing_clean_tariffs",
            "Lineage continuity is not yet convincing; prioritize docket-period and clean-sheet discovery.",
        )
    if unpaired_redline_doc_count > 0 and redline_clue_doc_count > 0:
        return (
            "use_redline_clues_to_find_clean_companions",
            "Redline documents contain actionable before/after clues but still lack a linked clean companion.",
        )
    if unpaired_redline_doc_count > 0:
        return (
            "link_redlines_to_clean_companions",
            "Redline evidence exists without a clearly linked clean companion for the same effective period.",
        )
    if anomaly_count > 0 or weak_parse_doc_count > 0:
        return (
            "inspect_profile_and_reparse",
            "Document inventory looks plausible, but parse output still shows sparse or weak extraction signals.",
        )
    if coverage_ratio < 1.0:
        return (
            "backfill_zero_charge_versions",
            "Versions exist but some still have no extracted charges.",
        )
    return (
        "likely_ok",
        "Timeline, document quality, and parse signals are aligned well enough for a high-confidence family.",
    )


def _write_csv(path: Path, rows: object) -> None:
    items = list(rows)  # type: ignore[arg-type]
    if not items:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(items[0].keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(items)


def _render_markdown(report: dict[str, object]) -> str:
    rows = list(report["rows"])  # type: ignore[arg-type]
    weakest = rows[:20]
    strongest = sorted(
        rows,
        key=lambda item: (
            float(item["confidence_score"]),
            float(item["coverage_pct"]),
        ),
        reverse=True,
    )[:10]
    redline_clue_rows = [
        row for row in rows
        if int(row.get("redline_clue_doc_count") or 0) > 0
    ][:15]

    lines = [
        "# NC Confidence Audit",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "Combines lineage continuity, gap signals, parse anomalies, document quality,",
        "and redline corroboration into a family-level confidence score.",
        "",
        "## Summary",
        "",
        f"- Total families: **{report['total_families']}**",
        "",
        "Confidence tiers:",
    ]
    for tier, count in dict(report["tier_counts"]).items():  # type: ignore[arg-type]
        lines.append(f"- `{tier}`: {count}")

    lines.extend(
        [
            "",
            "Recommended action counts:",
        ]
    )
    for action, count in dict(report["recommended_action_counts"]).items():  # type: ignore[arg-type]
        lines.append(f"- `{action}`: {count}")

    lines.extend(
        [
            "",
            "## Lowest Confidence Families",
            "",
            "| Score | Tier | Utility | Family | Timeline | Gaps | Anomalies | Redlines | Action |",
            "|---:|---|---|---|---|---:|---:|---:|---|",
        ]
    )
    for row in weakest:
        lines.append(
            "| "
            f"{row['confidence_score']:.1f} | {row['confidence_tier']} | {row['utility']} | "
            f"{row['family_key']} | {row['timeline_status']} | {row['gap_opportunity_count']} | "
            f"{row['anomaly_count']} | {row['redline_doc_count']} | {row['recommended_action']} |"
        )

    lines.extend(
        [
            "",
            "## Highest Confidence Families",
            "",
            "| Score | Tier | Utility | Family | Coverage | Timeline | Action |",
            "|---:|---|---|---|---:|---|---|",
        ]
    )
    for row in strongest:
        lines.append(
            "| "
            f"{row['confidence_score']:.1f} | {row['confidence_tier']} | {row['utility']} | "
            f"{row['family_key']} | {row['coverage_pct']:.1f}% | {row['timeline_status']} | "
            f"{row['recommended_action']} |"
        )

    if redline_clue_rows:
        lines.extend(
            [
                "",
                "## Families With Actionable Redline Clues",
                "",
                "| Score | Utility | Family | Redline Docs | Clue Docs | Supersession | Comparative | Action |",
                "|---:|---|---|---:|---:|---:|---:|---|",
            ]
        )
        for row in redline_clue_rows:
            lines.append(
                "| "
                f"{row['confidence_score']:.1f} | {row['utility']} | {row['family_key']} | "
                f"{row['redline_doc_count']} | {row['redline_clue_doc_count']} | "
                f"{row['supersession_clue_doc_count']} | {row['comparative_phrase_doc_count']} | "
                f"{row['recommended_action']} |"
            )

    return "\n".join(lines) + "\n"
