from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from duke_rates.db.repository import Repository


def _safe_json_load(payload: str | None) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


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


def _bounded_page_count(row: dict[str, Any]) -> int:
    try:
        start_page = int(row.get("start_page") or 0)
        end_page = int(row.get("end_page") or 0)
    except (TypeError, ValueError):
        return 0
    if start_page <= 0 or end_page < start_page:
        return 0
    return end_page - start_page + 1


def _normalization_lane(row: dict[str, Any]) -> str:
    title = str(row.get("title") or "").lower()
    page_count = int(row.get("page_count") or 0)
    layout_heavy = (
        page_count >= 5
        or "summary" in title
        or "compliance" in title
        or "book" in title
    )
    return "run_docling_or_paddle_structure" if layout_heavy else "queue_ocr_or_paddle"


def _classify_document_row(row: dict[str, Any]) -> tuple[str, str]:
    family_key = str(row.get("family_key") or "").lower()
    title = str(row.get("title") or "").lower()
    filing_classification = str(row.get("filing_classification") or "").lower()
    parser_profile = str(row.get("parser_profile") or "unknown")
    outcome_quality = str(row.get("outcome_quality") or "missing")
    processing_status = str(row.get("processing_status") or "")
    skip_reason = str(row.get("skip_reason") or "")
    charge_count = int(row.get("charge_count") or 0)
    stored_redline = bool(int(row.get("is_redline_candidate") or 0))

    if stored_redline:
        return ("redline_candidate", "stored redline fingerprint")

    # reference_only and formula_only skip reasons take precedence over stale charges —
    # old runs may have left charges before these families were classified as non-extractable.
    if skip_reason in {
        "reference_only_family",
        "reference_only_title",
    }:
        return ("reference_only", skip_reason or "parser marked reference-only")

    if skip_reason in {
        "formula_based_customer_specific_rider",
        "formula_only_family",
        "formula_only_program",
    } or parser_profile == "skipped_formula":
        return ("formula_only", skip_reason or "parser marked formula-only")

    if charge_count > 0:
        return ("extractable_charge", "parsed charges exist")

    if filing_classification in {"order", "testimony", "notice", "correspondence"}:
        return ("unrelated_but_keep", f"filing_classification={filing_classification}")

    if family_key.startswith("nc-") and "-doc-" in family_key:
        return ("unrelated_but_keep", "document family placeholder")

    reference_title_tokens = (
        "order",
        "testimony",
        "notice of hearing",
        "procedural",
        "motion",
        "application",
    )
    if any(token in title for token in reference_title_tokens) and "tariff" not in title:
        return ("reference_only", "reference/procedural title signal")

    if processing_status == "skipped" and outcome_quality in {"missing", "empty"}:
        return ("reference_only", "skipped without extractable charge output")

    if int(row.get("raw_text_chars") or 0) == 0:
        return ("needs_normalization", "no usable text; route through OCR/Paddle/Docling")

    if not processing_status:
        return ("needs_processing", "usable text exists but no latest processing run")

    if parser_profile != "unknown" and parser_profile != "generic_residential":
        return ("likely_extractable_specialized", f"specialized profile={parser_profile}")

    if parser_profile == "generic_residential" and outcome_quality in {"weak", "empty", "missing"}:
        return ("needs_better_routing", "generic fallback stayed weak/empty")

    if parser_profile == "unknown":
        return ("unknown", "no supported profile selected")

    return ("unknown", "no strong classification signal")


def build_document_classification_audit_report(
    repo: Repository,
    *,
    limit: int = 25,
    company: str | None = None,
    family_key: str | None = None,
) -> dict[str, Any]:
    with repo._connect() as conn:
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
            latest_fingerprints AS (
                SELECT df.*
                FROM document_fingerprints df
                JOIN (
                    SELECT source_pdf, MAX(id) AS max_id
                    FROM document_fingerprints
                    GROUP BY source_pdf
                ) latest
                  ON latest.max_id = df.id
            ),
            latest_discovery AS (
                SELECT dr.*
                FROM ncuc_discovery_records dr
                JOIN (
                    SELECT local_path, MAX(id) AS max_id
                    FROM ncuc_discovery_records
                    WHERE local_path IS NOT NULL
                    GROUP BY local_path
                ) latest
                  ON latest.max_id = dr.id
            ),
            page_text AS (
                SELECT
                    source_pdf,
                    file_hash,
                    SUM(text_length) AS page_artifact_text_chars
                FROM ncuc_page_artifacts
                GROUP BY source_pdf, file_hash
            ),
            version_charge_counts AS (
                SELECT tv.historical_document_id, COUNT(tc.id) AS charge_count
                FROM tariff_versions tv
                LEFT JOIN tariff_charges tc
                  ON tc.version_id = tv.id
                GROUP BY tv.historical_document_id
            )
            SELECT
                hd.id,
                hd.family_key,
                hd.company,
                hd.title,
                hd.local_path,
                hd.raw_text_path,
                hd.start_page,
                hd.end_page,
                hd.effective_start,
                COALESCE(vcc.charge_count, 0) AS charge_count,
                lr.status AS processing_status,
                lr.outcome_quality,
                lr.parser_profile,
                lr.metadata_json AS processing_metadata_json,
                COALESCE(lf.is_redline_candidate, 0) AS is_redline_candidate,
                COALESCE(lf.redline_confidence, 0.0) AS redline_confidence,
                lf.metadata_json AS fingerprint_metadata_json,
                ld.filing_classification,
                COALESCE(pt.page_artifact_text_chars, 0) AS page_artifact_text_chars
            FROM historical_documents hd
            LEFT JOIN latest_runs lr
              ON lr.historical_document_id = hd.id
            LEFT JOIN latest_fingerprints lf
              ON lf.source_pdf = hd.local_path
            LEFT JOIN latest_discovery ld
              ON ld.local_path = hd.local_path
            LEFT JOIN page_text pt
              ON pt.source_pdf = hd.local_path
             AND (pt.file_hash IS hd.content_hash OR pt.file_hash = hd.content_hash)
            LEFT JOIN version_charge_counts vcc
              ON vcc.historical_document_id = hd.id
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

    classified_rows: list[dict[str, Any]] = []
    bucket_counts: Counter[str] = Counter()
    parser_profile_counts: Counter[str] = Counter()

    for sqlite_row in rows:
        row = dict(sqlite_row)
        processing_metadata = _safe_json_load(row.get("processing_metadata_json"))
        fingerprint_metadata = _safe_json_load(row.get("fingerprint_metadata_json"))
        selection_metadata = processing_metadata.get("selection") or {}
        skip_reason = (
            processing_metadata.get("skip_reason")
            or selection_metadata.get("skip_reason")
            or fingerprint_metadata.get("skip_reason")
            or ""
        )
        row["skip_reason"] = skip_reason
        row["raw_text_chars"] = max(
            _safe_text_file_length(row.get("raw_text_path")),
            int(row.get("page_artifact_text_chars") or 0),
        )
        row["page_count"] = _bounded_page_count(row)
        bucket, reason = _classify_document_row(row)
        bucket_counts[bucket] += 1
        parser_profile_counts[str(row.get("parser_profile") or "unknown")] += 1
        classified_rows.append(
            {
                "historical_document_id": int(row["id"]),
                "family_key": row["family_key"],
                "company": row["company"],
                "title": row["title"],
                "effective_start": row["effective_start"],
                "charge_count": int(row["charge_count"] or 0),
                "raw_text_chars": int(row["raw_text_chars"] or 0),
                "page_count": int(row["page_count"] or 0),
                "normalization_lane": _normalization_lane(row),
                "processing_status": row["processing_status"],
                "outcome_quality": row["outcome_quality"],
                "parser_profile": row["parser_profile"] or "unknown",
                "filing_classification": row["filing_classification"],
                "document_bucket": bucket,
                "classification_reason": reason,
                "is_redline_candidate": bool(int(row["is_redline_candidate"] or 0)),
                "redline_confidence": round(float(row["redline_confidence"] or 0.0), 4),
                "skip_reason": skip_reason or None,
            }
        )

    priority_rank = {
        "needs_normalization": 0,
        "needs_processing": 1,
        "unknown": 2,
        "needs_better_routing": 3,
        "likely_extractable_specialized": 4,
        "reference_only": 5,
        "formula_only": 6,
        "redline_candidate": 7,
        "unrelated_but_keep": 8,
        "extractable_charge": 9,
    }
    classified_rows.sort(
        key=lambda item: (
            priority_rank.get(str(item["document_bucket"]), 99),
            0 if item["charge_count"] == 0 else 1,
            str(item["family_key"] or ""),
            int(item["historical_document_id"]),
        )
    )

    return {
        "summary": {
            "historical_document_count": len(classified_rows),
            "bucket_counts": [
                {"document_bucket": name, "count": count}
                for name, count in bucket_counts.most_common()
            ],
            "top_parser_profiles": [
                {"parser_profile": name, "count": count}
                for name, count in parser_profile_counts.most_common(10)
            ],
        },
        "rows": classified_rows[:limit],
    }


def _derive_unknown_routing_action(rows: list[dict[str, Any]]) -> tuple[str, str]:
    buckets = Counter(str(row.get("document_bucket") or "") for row in rows)
    if buckets and buckets.most_common(1)[0][0] == "needs_normalization":
        return (
            "enqueue_ocr_remediation",
            "documents have no usable text; recover normalized text before parser work",
        )
    if buckets and buckets.most_common(1)[0][0] == "needs_processing":
        return (
            "enqueue_reprocess",
            "usable text exists but documents have no latest processing run",
        )

    filing_classes = Counter(
        str(row.get("filing_classification") or "").lower()
        for row in rows
        if row.get("filing_classification")
    )
    titles = " ".join(str(row.get("title") or "").lower() for row in rows)
    if filing_classes and set(filing_classes).issubset({"order", "testimony", "exhibit", "other"}):
        return (
            "reclassify_non_tariff_or_reference",
            "discovery filing classification skews non-tariff/procedural",
        )
    if any(token in titles for token in ("prospective rider", "program benefits", "pilot", "compliance tariff")):
        return (
            "evaluate_formula_or_program_lane",
            "title signals suggest program/formula or compliance content rather than standard charges",
        )
    if any(token in titles for token in ("summary of rider adjustments", "billing adjustment", "adjustments")):
        return (
            "map_to_adjustment_or_matrix_profile",
            "title signals suggest rider-adjustment or billing-adjustment structure",
        )
    if any(token in titles for token in ("service regulations", "order", "application")):
        return (
            "reclassify_reference_or_unrelated",
            "title signals suggest reference/procedural content",
        )
    return (
        "new_profile_or_family_routing_review",
        "unsupported family needs explicit routing or a dedicated profile",
    )


def _synthesize_routing_profile(
    *,
    family_key: str,
    company: str | None,
    title: str,
    recommended_action: str,
    reason: str,
) -> dict[str, Any]:
    family_key_l = family_key.lower()
    company_l = str(company or "").lower()
    title_l = title.lower()

    candidate_profile: str | None = None
    synthesis_kind = "manual_review"
    synthesis_reason = reason

    if recommended_action in {"enqueue_ocr_remediation", "enqueue_reprocess"}:
        return {
            "synthesized_profile_name": None,
            "synthesized_profile_kind": "queue_only",
            "synthesized_profile_reason": synthesis_reason,
            "synthesized_next_command": None,
        }

    if (
        "billing adjustments" in title_l
        or "summary of rider adjustments" in title_l
        or family_key_l == "nc-progress-leaf-601"
    ):
        candidate_profile = "progress_billing_adjustments" if company_l == "progress" else "progress_rider_adjustment_matrix"
        synthesis_kind = "existing_profile"
        synthesis_reason = "billing-adjustment summary/matrix language"
    elif "management and energy efficiency cost recovery rider" in title_l or "managementandenergyefficiencycostrecoveryrider" in family_key_l:
        candidate_profile = "progress_management_energy_efficiency_cost_recovery_rider"
        synthesis_kind = "existing_profile"
        synthesis_reason = "management and energy-efficiency cost recovery rider language"
    elif "compliance report and cost recovery rider" in title_l or "compliancereportandcostrecoveryrider" in family_key_l:
        candidate_profile = "progress_compliance_report_and_cost_recovery_rider"
        synthesis_kind = "existing_profile"
        synthesis_reason = "compliance report and cost recovery rider language"
    elif "recovery rider" in title_l or "recoveryrider" in family_key_l:
        candidate_profile = "progress_recovery_rider"
        synthesis_kind = "existing_profile"
        synthesis_reason = "recovery rider language"
    elif any(
        token in title_l
        for token in (
            "appliance recycling program",
            "lighting program",
            "efficiency program",
            "demand response program",
            "construction program",
            "schools program",
            "weatherization assistance program",
            "appendix c program",
            "program",
        )
    ) or "program" in family_key_l:
        candidate_profile = "zero_charge_program"
        synthesis_kind = "existing_profile"
        synthesis_reason = "program/reference sheet language"
    elif any(
        token in title_l
        for token in (
            "agency asset rider",
            "single value rider",
            "load control",
            "income-qualified load control",
            "monthly rate",
            "approved rate",
        )
    ) or "rider" in family_key_l:
        if company_l == "carolinas" and "prospective rider" in title_l:
            candidate_profile = "carolinas_prospective_rider"
            synthesis_kind = "new_profile_candidate"
            synthesis_reason = "carolinas prospective rider language"
        elif company_l == "carolinas" and "lighting" in title_l:
            candidate_profile = "carolinas_lighting_schedule"
            synthesis_kind = "existing_profile"
            synthesis_reason = "carolinas lighting schedule language"
        else:
            candidate_profile = "progress_single_value_rider" if company_l == "progress" else "carolinas_single_value_rider"
            synthesis_kind = "existing_profile"
            synthesis_reason = "single-value rider language"

    next_command = None
    if candidate_profile and synthesis_kind == "existing_profile":
        next_command = (
            "python -m duke_rates enqueue-profile-impact-nc "
            f"--parser-profile {candidate_profile} --limit 25 --requested-by unknown_routing_audit"
        )

    return {
        "synthesized_profile_name": candidate_profile,
        "synthesized_profile_kind": synthesis_kind,
        "synthesized_profile_reason": synthesis_reason,
        "synthesized_next_command": next_command,
    }


def build_unknown_routing_audit_report(
    repo: Repository,
    *,
    limit: int = 25,
    company: str | None = None,
) -> dict[str, Any]:
    base = build_document_classification_audit_report(
        repo,
        limit=5000,
        company=company,
    )
    problem_rows = [
        row
        for row in base["rows"]
        if row["document_bucket"] in {
            "unknown",
            "needs_normalization",
            "needs_processing",
            "needs_better_routing",
        }
    ]
    by_family: dict[str, list[dict[str, Any]]] = {}
    for row in problem_rows:
        key = str(row.get("family_key") or "(missing-family)")
        by_family.setdefault(key, []).append(row)

    ranked_rows: list[dict[str, Any]] = []
    for family_key, rows in by_family.items():
        parser_profiles = Counter(str(row.get("parser_profile") or "unknown") for row in rows)
        filing_classes = Counter(
            str(row.get("filing_classification") or "unknown")
            for row in rows
        )
        buckets = Counter(str(row.get("document_bucket") or "unknown") for row in rows)
        action, reason = _derive_unknown_routing_action(rows)
        action_bucket = {
            "enqueue_ocr_remediation": "needs_normalization",
            "enqueue_reprocess": "needs_processing",
        }.get(action)
        action_rows = [
            row for row in rows
            if action_bucket is None or row.get("document_bucket") == action_bucket
        ]
        ranked_rows.append(
            {
                "family_key": family_key,
                "document_count": len(rows),
                "company": rows[0].get("company"),
                "document_buckets": dict(sorted(buckets.items())),
                "top_parser_profile": parser_profiles.most_common(1)[0][0],
                "top_filing_classification": filing_classes.most_common(1)[0][0],
                "top_normalization_lane": Counter(
                    str(row.get("normalization_lane") or "queue_ocr_or_paddle")
                    for row in rows
                ).most_common(1)[0][0],
                "historical_document_ids": [
                    int(row["historical_document_id"])
                    for row in sorted(rows, key=lambda item: int(item["historical_document_id"]))
                ],
                "action_historical_document_ids": [
                    int(row["historical_document_id"])
                    for row in sorted(action_rows, key=lambda item: int(item["historical_document_id"]))
                ],
                "sample_title": rows[0].get("title"),
                "recommended_action": action,
                "reason": reason,
                **_synthesize_routing_profile(
                    family_key=family_key,
                    company=rows[0].get("company"),
                    title=str(rows[0].get("title") or ""),
                    recommended_action=action,
                    reason=reason,
                ),
            }
        )

    action_counts = Counter(str(row["recommended_action"]) for row in ranked_rows)
    ranked_rows.sort(
        key=lambda row: (
            -int(row["document_count"]),
            str(row["recommended_action"]),
            str(row["family_key"]),
        )
    )
    return {
        "summary": {
            "problem_document_count": len(problem_rows),
            "problem_family_count": len(ranked_rows),
            "recommended_action_counts": [
                {"recommended_action": name, "count": count}
                for name, count in action_counts.most_common()
            ],
        },
        "rows": ranked_rows[:limit],
    }
