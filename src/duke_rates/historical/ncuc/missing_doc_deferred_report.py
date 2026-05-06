from __future__ import annotations

import json
from collections import Counter
from typing import Any

from duke_rates.db.repository import Repository
from duke_rates.config import Settings
from duke_rates.historical.ncuc.missing_doc_remediation import (
    remediate_and_promote_missing_doc_targets,
)

_ACTIONABLE_REASON_WEIGHTS: dict[str, float] = {
    "no_downloadable_url": 1.0,
    "missing_effective_start_for_weak_match": 0.9,
    "confidence_below_threshold": 0.75,
}


def build_nc_missing_doc_deferred_report(
    repository: Repository,
    *,
    family_key: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    discovery_rows = []
    for row in repository.list_ncuc_discovery_records(family_key=family_key):
        metadata = _loads_json_object(getattr(row, "metadata_json", None))
        workflow = _loads_json_object(metadata.get("missing_doc_workflow"))
        promotion = _loads_json_object(workflow.get("search_promotion"))
        if not promotion or bool(promotion.get("promotable")):
            continue
        reasons = [str(item) for item in promotion.get("reasons", []) if str(item).strip()]
        discovery_rows.append(
            {
                "id": row.id,
                "family_keys": list(getattr(row, "family_keys", []) or []),
                "docket_number": getattr(row, "docket_number", None),
                "filing_title": getattr(row, "filing_title", None),
                "filing_date": getattr(row, "filing_date", None),
                "search_confidence_score": promotion.get("search_confidence_score"),
                "search_ideality": promotion.get("search_ideality"),
                "reasons": reasons,
            }
        )

    historical_rows = []
    docs = repository.list_historical_documents(state="NC")
    if family_key:
        docs = [doc for doc in docs if getattr(doc, "family_key", None) == family_key]
    for row in docs:
        metadata = _loads_json_object(getattr(row, "metadata_json", None))
        workflow = _loads_json_object(metadata.get("missing_doc_workflow"))
        promotion = _loads_json_object(workflow.get("import_promotion"))
        if not promotion or bool(promotion.get("promotable")):
            continue
        reasons = [str(item) for item in promotion.get("reasons", []) if str(item).strip()]
        historical_rows.append(
            {
                "id": row.id,
                "family_key": getattr(row, "family_key", None),
                "title": getattr(row, "title", None),
                "effective_start": getattr(row, "effective_start", None),
                "family_match_score": promotion.get("family_match_score"),
                "start_page": getattr(row, "start_page", None),
                "end_page": getattr(row, "end_page", None),
                "reasons": reasons,
            }
        )

    discovery_rows = discovery_rows[:limit]
    historical_rows = historical_rows[:limit]

    return {
        "family_key": family_key,
        "summary": {
            "deferred_discovery_count": len(discovery_rows),
            "deferred_historical_count": len(historical_rows),
            "discovery_reason_summary": _reason_summary(discovery_rows),
            "historical_reason_summary": _reason_summary(historical_rows),
            "combined_reason_summary": _combined_reason_summary(discovery_rows, historical_rows),
        },
        "deferred_discovery_records": discovery_rows,
        "deferred_historical_documents": historical_rows,
    }


def build_nc_missing_doc_remediation_plan(
    repository: Repository,
    *,
    family_key: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    report = build_nc_missing_doc_deferred_report(
        repository,
        family_key=family_key,
        limit=limit,
    )
    actionable: dict[str, dict[str, Any]] = {}
    for row in report["deferred_discovery_records"]:
        for reason in row.get("reasons", []):
            normalized = _normalize_reason(reason)
            if normalized not in _ACTIONABLE_REASON_WEIGHTS:
                continue
            bucket = actionable.setdefault(
                normalized,
                {
                    "reason": normalized,
                    "scope": "discovery",
                    "count": 0,
                    "family_keys": set(),
                    "example_ids": [],
                },
            )
            bucket["count"] += 1
            bucket["family_keys"].update(str(item) for item in row.get("family_keys", []) if str(item).strip())
            if len(bucket["example_ids"]) < 5 and row.get("id") is not None:
                bucket["example_ids"].append(int(row["id"]))
    for row in report["deferred_historical_documents"]:
        for reason in row.get("reasons", []):
            normalized = _normalize_reason(reason)
            if normalized not in _ACTIONABLE_REASON_WEIGHTS:
                continue
            bucket = actionable.setdefault(
                normalized,
                {
                    "reason": normalized,
                    "scope": "historical",
                    "count": 0,
                    "family_keys": set(),
                    "example_ids": [],
                },
            )
            if bucket["scope"] != "historical":
                bucket["scope"] = "mixed"
            bucket["count"] += 1
            if row.get("family_key"):
                bucket["family_keys"].add(str(row["family_key"]))
            if len(bucket["example_ids"]) < 5 and row.get("id") is not None:
                bucket["example_ids"].append(int(row["id"]))

    ranked_steps = []
    for reason, bucket in actionable.items():
        weight = _ACTIONABLE_REASON_WEIGHTS[reason]
        step = {
            "reason": reason,
            "scope": bucket["scope"],
            "count": bucket["count"],
            "weighted_score": round(bucket["count"] * weight, 2),
            "family_keys": sorted(bucket["family_keys"]),
            "example_ids": list(bucket["example_ids"]),
            "recommended_command": _recommended_command(reason, family_key=family_key),
        }
        ranked_steps.append(step)
    ranked_steps.sort(key=lambda item: (float(item["weighted_score"]), int(item["count"])), reverse=True)

    return {
        "family_key": family_key,
        "summary": report["summary"],
        "ranked_steps": ranked_steps,
    }


def execute_top_nc_missing_doc_remediation_step(
    settings: Settings,
    repository: Repository,
    *,
    family_key: str | None = None,
    limit: int = 100,
    promotion_min_ideality: str = "probable",
    promotion_min_confidence: float = 45.0,
    import_promotion_min_family_score: float = 24.0,
    requested_by: str = "workflow",
) -> dict[str, Any]:
    before_plan = build_nc_missing_doc_remediation_plan(
        repository,
        family_key=family_key,
        limit=limit,
    )
    top_step = before_plan["ranked_steps"][0] if before_plan["ranked_steps"] else None
    if not top_step:
        report = {
            "family_key": family_key,
            "executed": False,
            "selected_step": None,
            "before_plan": before_plan,
            "after_plan": before_plan,
            "execution_report": {},
        }
        _record_execution_history(repository, report=report, requested_by=requested_by)
        return report

    execution_report = remediate_and_promote_missing_doc_targets(
        settings,
        repository,
        family_key=family_key,
        reasons=[str(top_step["reason"])],
        limit=limit,
        promotion_min_ideality=promotion_min_ideality,
        promotion_min_confidence=promotion_min_confidence,
        import_promotion_min_family_score=import_promotion_min_family_score,
        requested_by=requested_by,
    )
    after_plan = build_nc_missing_doc_remediation_plan(
        repository,
        family_key=family_key,
        limit=limit,
    )
    report = {
        "family_key": family_key,
        "executed": True,
        "selected_step": top_step,
        "before_plan": before_plan,
        "after_plan": after_plan,
        "execution_report": execution_report,
    }
    _record_execution_history(repository, report=report, requested_by=requested_by)
    return report


def _reason_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for row in rows:
        counter.update(row.get("reasons", []))
    return [
        {"reason": reason, "count": count}
        for reason, count in counter.most_common()
    ]


def _combined_reason_summary(
    discovery_rows: list[dict[str, Any]],
    historical_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    scoped: dict[str, dict[str, int]] = {}
    for scope_name, rows in (
        ("discovery", discovery_rows),
        ("historical", historical_rows),
    ):
        for row in rows:
            for reason in row.get("reasons", []):
                counter[reason] += 1
                scope_counts = scoped.setdefault(reason, {"discovery": 0, "historical": 0})
                scope_counts[scope_name] += 1
    return [
        {
            "reason": reason,
            "count": count,
            "discovery_count": scoped.get(reason, {}).get("discovery", 0),
            "historical_count": scoped.get(reason, {}).get("historical", 0),
        }
        for reason, count in counter.most_common()
    ]


def _loads_json_object(payload: Any) -> dict[str, Any]:
    if not payload:
        return {}
    if isinstance(payload, dict):
        return payload
    try:
        loaded = json.loads(str(payload))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _normalize_reason(reason: str) -> str:
    value = str(reason or "").strip()
    if value.startswith("confidence_below_threshold:"):
        return "confidence_below_threshold"
    return value


def _recommended_command(reason: str, *, family_key: str | None) -> str:
    family_arg = f" --family-key {family_key}" if family_key else ""
    if reason == "no_downloadable_url":
        return f"python -m duke_rates remediate-and-promote-nc-missing-docs --reason no_downloadable_url{family_arg}"
    if reason == "missing_effective_start_for_weak_match":
        return f"python -m duke_rates remediate-and-promote-nc-missing-docs --reason missing_effective_start_for_weak_match{family_arg}"
    if reason == "confidence_below_threshold":
        return f"python -m duke_rates remediate-and-promote-nc-missing-docs --reason confidence_below_threshold{family_arg}"
    return ""


def _record_execution_history(
    repository: Repository,
    *,
    report: dict[str, Any],
    requested_by: str,
) -> None:
    if not hasattr(repository, "record_missing_doc_remediation_run"):
        return
    selected = report.get("selected_step") or {}
    before_plan = report.get("before_plan") or {}
    after_plan = report.get("after_plan") or {}
    before_summary = before_plan.get("summary") or {}
    after_summary = after_plan.get("summary") or {}
    metadata = {
        "selected_step": selected,
        "execution_report": report.get("execution_report") or {},
        "before_plan": before_plan,
        "after_plan": after_plan,
    }
    repository.record_missing_doc_remediation_run(
        family_key=report.get("family_key"),
        selected_reason=selected.get("reason"),
        selected_scope=selected.get("scope"),
        selected_weighted_score=selected.get("weighted_score"),
        executed=bool(report.get("executed")),
        before_step_count=len(before_plan.get("ranked_steps") or []),
        after_step_count=len(after_plan.get("ranked_steps") or []),
        before_deferred_discovery_count=int(before_summary.get("deferred_discovery_count") or 0),
        before_deferred_historical_count=int(before_summary.get("deferred_historical_count") or 0),
        after_deferred_discovery_count=int(after_summary.get("deferred_discovery_count") or 0),
        after_deferred_historical_count=int(after_summary.get("deferred_historical_count") or 0),
        requested_by=requested_by,
        metadata=metadata,
    )


__all__ = [
    "build_nc_missing_doc_deferred_report",
    "build_nc_missing_doc_remediation_plan",
    "execute_top_nc_missing_doc_remediation_step",
]
