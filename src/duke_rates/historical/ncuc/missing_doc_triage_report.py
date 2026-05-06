from __future__ import annotations

import json
from typing import Any

from duke_rates.config import Settings
from duke_rates.db.repository import Repository


def build_nc_missing_doc_triage_report(
    repository: Repository,
    *,
    family_key: str | None = None,
    limit: int = 100,
    actionable_only: bool = False,
    top: int | None = None,
) -> dict[str, Any]:
    discovery_rows = _load_discovery_rows(repository, family_key=family_key, limit=limit)
    historical_rows = _load_historical_rows(repository, family_key=family_key, limit=limit)

    next_action_counts: dict[str, int] = {}
    blocked_reason_counts: dict[str, int] = {}
    combined_rows = [*discovery_rows, *historical_rows]
    for row in combined_rows:
        action = str(row.get("next_action") or "unknown")
        next_action_counts[action] = next_action_counts.get(action, 0) + 1
        blocked_reason = str(row.get("blocked_reason") or "").strip()
        if blocked_reason:
            blocked_reason_counts[blocked_reason] = blocked_reason_counts.get(blocked_reason, 0) + 1

    ranked_targets = []
    for row in combined_rows:
        ranked = dict(row)
        ranked["priority_score"] = _triage_priority_score(ranked)
        ranked["suggested_command"] = _suggested_command_for_target(ranked)
        ranked_targets.append(ranked)
    ranked_targets.sort(
        key=lambda row: (
            -int(row.get("priority_score") or 0),
            int(row.get("id") or 0),
        ),
    )
    if actionable_only:
        ranked_targets = [row for row in ranked_targets if _is_actionable_target(row)]
    if top is not None and top > 0:
        ranked_targets = ranked_targets[:top]

    return {
        "family_key": family_key,
        "summary": {
            "discovery_triage_count": len(discovery_rows),
            "historical_triage_count": len(historical_rows),
            "combined_triage_count": len(combined_rows),
            "ranked_target_count": len(ranked_targets),
            "next_action_summary": _sorted_count_rows(next_action_counts, label="next_action"),
            "blocked_reason_summary": _sorted_count_rows(blocked_reason_counts, label="blocked_reason"),
        },
        "discovery_records": discovery_rows[:limit],
        "historical_documents": historical_rows[:limit],
        "combined_targets": combined_rows[:limit],
        "ranked_targets": ranked_targets,
    }


def execute_top_nc_missing_doc_triage_action(
    settings: Settings,
    repository: Repository,
    *,
    family_key: str | None = None,
    limit: int = 100,
    requested_by: str = "workflow",
) -> dict[str, Any]:
    before_report = build_nc_missing_doc_triage_report(
        repository,
        family_key=family_key,
        limit=limit,
        actionable_only=True,
        top=1,
    )
    top_target = next(iter(before_report.get("ranked_targets", [])), None)
    if not top_target:
        return {
            "family_key": family_key,
            "executed": False,
            "selected_target": None,
            "before_report": before_report,
            "after_report": before_report,
            "execution_report": {},
        }

    execution_report = _execute_target_action(
        settings,
        repository,
        target=top_target,
        requested_by=requested_by,
    )
    after_report = build_nc_missing_doc_triage_report(
        repository,
        family_key=family_key,
        limit=limit,
        actionable_only=True,
        top=1,
    )
    return {
        "family_key": family_key,
        "executed": True,
        "selected_target": top_target,
        "before_report": before_report,
        "after_report": after_report,
        "execution_report": execution_report,
    }


def execute_batch_nc_missing_doc_triage_actions(
    settings: Settings,
    repository: Repository,
    *,
    family_key: str | None = None,
    limit: int = 100,
    max_actions: int = 5,
    requested_by: str = "workflow",
) -> dict[str, Any]:
    initial_report = build_nc_missing_doc_triage_report(
        repository,
        family_key=family_key,
        limit=limit,
        actionable_only=True,
    )
    steps: list[dict[str, Any]] = []
    seen_targets: set[tuple[str, int]] = set()
    stop_reason = "max_actions_reached"

    for _ in range(max(0, int(max_actions))):
        current_report = build_nc_missing_doc_triage_report(
            repository,
            family_key=family_key,
            limit=limit,
            actionable_only=True,
            top=1,
        )
        top_target = next(iter(current_report.get("ranked_targets", [])), None)
        if not top_target:
            stop_reason = "no_actionable_targets"
            break
        target_key = (
            str(top_target.get("target_type") or ""),
            int(top_target.get("id") or 0),
        )
        if target_key in seen_targets:
            stop_reason = "repeat_target_detected"
            break
        seen_targets.add(target_key)

        step_report = execute_top_nc_missing_doc_triage_action(
            settings,
            repository,
            family_key=family_key,
            limit=limit,
            requested_by=requested_by,
        )
        steps.append(step_report)

        before_count = int((step_report.get("before_report", {}).get("summary") or {}).get("ranked_target_count") or 0)
        after_count = int((step_report.get("after_report", {}).get("summary") or {}).get("ranked_target_count") or 0)
        if after_count >= before_count:
            stop_reason = "no_progress_after_step"
            break
    else:
        stop_reason = "max_actions_reached"

    final_report = build_nc_missing_doc_triage_report(
        repository,
        family_key=family_key,
        limit=limit,
        actionable_only=True,
    )
    return {
        "family_key": family_key,
        "executed_count": len(steps),
        "max_actions": int(max_actions),
        "stop_reason": stop_reason,
        "initial_report": initial_report,
        "final_report": final_report,
        "steps": steps,
    }


def _load_discovery_rows(
    repository: Repository,
    *,
    family_key: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in repository.list_ncuc_discovery_records(family_key=family_key):
        metadata = _loads_json_object(getattr(record, "metadata_json", None))
        workflow = _loads_json_object(metadata.get("missing_doc_workflow"))
        triage = _loads_json_object(workflow.get("triage"))
        if not triage or triage.get("scope") != "discovery_record":
            continue
        triage_family = str(triage.get("family_key") or "").strip()
        record_families = [str(item) for item in getattr(record, "family_keys", [])]
        if family_key and family_key not in record_families and family_key != triage_family:
            continue
        rows.append(
            {
                "target_type": "discovery_record",
                "id": int(record.id),
                "family_key": triage_family or (record_families[0] if record_families else None),
                "next_action": triage.get("next_action"),
                "blocked_reason": triage.get("blocked_reason"),
                "updated_at": triage.get("updated_at"),
                "fetch_status": triage.get("fetch_status") or getattr(record, "fetch_status", None),
                "docket_number": getattr(record, "docket_number", None),
                "filing_title": getattr(record, "filing_title", None),
                "linked_historical_document_ids": list(triage.get("linked_historical_document_ids") or []),
            }
        )
    rows.sort(key=lambda row: (str(row.get("updated_at") or ""), int(row["id"])), reverse=True)
    return rows[:limit]


def _load_historical_rows(
    repository: Repository,
    *,
    family_key: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for doc in repository.list_historical_documents(state="NC"):
        if family_key and getattr(doc, "family_key", None) != family_key:
            metadata = _loads_json_object(getattr(doc, "metadata_json", None))
            workflow = _loads_json_object(metadata.get("missing_doc_workflow"))
            triage = _loads_json_object(workflow.get("triage"))
            if str(triage.get("family_key") or "").strip() != family_key:
                continue
        else:
            metadata = _loads_json_object(getattr(doc, "metadata_json", None))
            workflow = _loads_json_object(metadata.get("missing_doc_workflow"))
            triage = _loads_json_object(workflow.get("triage"))
        if not triage or triage.get("scope") != "historical_document":
            continue
        rows.append(
            {
                "target_type": "historical_document",
                "id": int(doc.id),
                "family_key": triage.get("family_key") or getattr(doc, "family_key", None),
                "next_action": triage.get("next_action"),
                "blocked_reason": triage.get("blocked_reason"),
                "updated_at": triage.get("updated_at"),
                "current_stage": triage.get("current_stage"),
                "latest_run_status": triage.get("latest_run_status"),
                "latest_outcome_quality": triage.get("latest_outcome_quality"),
                "latest_review_outcome": triage.get("latest_review_outcome"),
                "latest_queue_status": triage.get("latest_queue_status"),
                "effective_start": getattr(doc, "effective_start", None),
                "title": getattr(doc, "title", None),
            }
        )
    rows.sort(key=lambda row: (str(row.get("updated_at") or ""), int(row["id"])), reverse=True)
    return rows[:limit]


def _sorted_count_rows(counts: dict[str, int], *, label: str) -> list[dict[str, Any]]:
    rows = [{label: key, "count": value} for key, value in counts.items()]
    rows.sort(key=lambda row: (-int(row["count"]), str(row[label])))
    return rows


def _is_actionable_target(row: dict[str, Any]) -> bool:
    action = str(row.get("next_action") or "").strip().lower()
    if not action:
        return False
    return action not in {"monitor_linked_document", "wait_for_reprocess_completion", "ready_for_acceptance"}


def _triage_priority_score(row: dict[str, Any]) -> int:
    action = str(row.get("next_action") or "").strip().lower()
    blocked = str(row.get("blocked_reason") or "").strip().lower()
    target_type = str(row.get("target_type") or "").strip().lower()
    current_stage = str(row.get("current_stage") or "").strip().lower()
    outcome_quality = str(row.get("latest_outcome_quality") or "").strip().lower()

    action_scores = {
        "review_parse_output": 100,
        "retry_with_better_parser_context": 95,
        "review_family_assignment": 90,
        "bootstrap_tariff_version": 88,
        "retry_fetch_or_manual_portal_review": 82,
        "import_and_mine_document": 80,
        "fetch_document": 75,
        "process_document": 72,
        "ready_for_acceptance": 40,
        "wait_for_reprocess_completion": 15,
        "monitor_linked_document": 10,
    }
    blocked_bonus = 0
    if blocked in {"needs_review", "processed_empty", "processed_weak", "fetch_failed"}:
        blocked_bonus = 12
    elif blocked:
        blocked_bonus = 6
    stage_bonus = 0
    if current_stage == "needs_review":
        stage_bonus = 8
    elif outcome_quality in {"empty", "weak"}:
        stage_bonus = 6
    type_bonus = 4 if target_type == "historical_document" else 0
    return int(action_scores.get(action, 50) + blocked_bonus + stage_bonus + type_bonus)


def _suggested_command_for_target(row: dict[str, Any]) -> str | None:
    target_type = str(row.get("target_type") or "").strip().lower()
    next_action = str(row.get("next_action") or "").strip().lower()
    family_key = str(row.get("family_key") or "").strip()
    target_id = row.get("id")

    family_arg = f" --family-key {family_key}" if family_key else ""
    if target_type == "discovery_record" and target_id is not None:
        target_arg = f" --record-id {int(target_id)}"
    elif target_type == "historical_document" and target_id is not None:
        target_arg = f" --historical-document-id {int(target_id)}"
    else:
        target_arg = ""

    command_map = {
        "fetch_document": f"python -m duke_rates run-nc-missing-doc-workflow --from-stage fetch --to-stage fetch{target_arg}{family_arg}",
        "retry_fetch_or_manual_portal_review": f"python -m duke_rates run-nc-missing-doc-workflow --from-stage fetch --to-stage fetch --retry-failed-fetch{target_arg}{family_arg}",
        "import_and_mine_document": f"python -m duke_rates run-nc-missing-doc-workflow --from-stage import --to-stage import{target_arg}{family_arg}",
        "bootstrap_tariff_version": f"python -m duke_rates run-nc-missing-doc-workflow --from-stage bootstrap_versions --to-stage bootstrap_versions{target_arg}{family_arg}",
        "review_family_assignment": f"python -m duke_rates show-nc-missing-doc-status{target_arg}{family_arg}",
        "process_document": f"python -m duke_rates run-nc-missing-doc-workflow --from-stage queue_reprocess --to-stage process_reprocess{target_arg}{family_arg}",
        "retry_with_better_parser_context": f"python -m duke_rates run-nc-missing-doc-workflow --from-stage queue_reprocess --to-stage process_reprocess{target_arg}{family_arg}",
        "review_parse_output": f"python -m duke_rates show-nc-missing-doc-status{target_arg}{family_arg}",
        "ready_for_acceptance": f"python -m duke_rates show-nc-missing-doc-status{target_arg}{family_arg}",
        "wait_for_reprocess_completion": f"python -m duke_rates show-nc-missing-doc-status{target_arg}{family_arg}",
        "monitor_linked_document": f"python -m duke_rates show-nc-missing-doc-status{target_arg}{family_arg}",
    }
    command = command_map.get(next_action)
    return " ".join(str(command or "").split()) or None


def _execute_target_action(
    settings: Settings,
    repository: Repository,
    *,
    target: dict[str, Any],
    requested_by: str,
) -> dict[str, Any]:
    from duke_rates.historical.ncuc.missing_doc_status import (
        build_nc_missing_doc_status_report,
    )
    from duke_rates.historical.ncuc.missing_doc_workflow import (
        run_nc_missing_doc_workflow,
    )

    next_action = str(target.get("next_action") or "").strip().lower()
    family_key = str(target.get("family_key") or "").strip() or None
    target_id = int(target.get("id") or 0) or None
    target_type = str(target.get("target_type") or "").strip().lower()

    workflow_kwargs: dict[str, Any] = {
        "family_key": family_key,
        "limit": 1,
        "requested_by": requested_by,
    }
    if target_type == "discovery_record" and target_id is not None:
        workflow_kwargs["discovery_record_ids"] = [target_id]
    if target_type == "historical_document" and target_id is not None:
        workflow_kwargs["historical_document_ids"] = [target_id]

    if next_action == "fetch_document":
        return {
            "action": next_action,
            "workflow_report": run_nc_missing_doc_workflow(
                settings,
                repository,
                from_stage="fetch",
                to_stage="fetch",
                **workflow_kwargs,
            ),
        }
    if next_action == "retry_fetch_or_manual_portal_review":
        return {
            "action": next_action,
            "workflow_report": run_nc_missing_doc_workflow(
                settings,
                repository,
                from_stage="fetch",
                to_stage="fetch",
                fetch_retry_failed=True,
                **workflow_kwargs,
            ),
        }
    if next_action == "import_and_mine_document":
        return {
            "action": next_action,
            "workflow_report": run_nc_missing_doc_workflow(
                settings,
                repository,
                from_stage="import",
                to_stage="import",
                **workflow_kwargs,
            ),
        }
    if next_action == "bootstrap_tariff_version":
        return {
            "action": next_action,
            "workflow_report": run_nc_missing_doc_workflow(
                settings,
                repository,
                from_stage="bootstrap_versions",
                to_stage="bootstrap_versions",
                **workflow_kwargs,
            ),
        }
    if next_action in {"process_document", "retry_with_better_parser_context"}:
        return {
            "action": next_action,
            "workflow_report": run_nc_missing_doc_workflow(
                settings,
                repository,
                from_stage="queue_reprocess",
                to_stage="process_reprocess",
                **workflow_kwargs,
            ),
        }
    if next_action in {
        "review_family_assignment",
        "review_parse_output",
        "ready_for_acceptance",
        "wait_for_reprocess_completion",
        "monitor_linked_document",
    }:
        status_kwargs: dict[str, Any] = {}
        if target_type == "discovery_record" and target_id is not None:
            status_kwargs["discovery_record_id"] = target_id
        elif target_type == "historical_document" and target_id is not None:
            status_kwargs["historical_document_id"] = target_id
        else:
            status_kwargs["family_key"] = family_key
        return {
            "action": next_action,
            "status_report": build_nc_missing_doc_status_report(repository, **status_kwargs),
        }
    return {
        "action": next_action,
        "skipped": True,
        "reason": "unsupported_next_action",
    }


def _loads_json_object(payload: Any) -> dict[str, Any]:
    if not payload:
        return {}
    if isinstance(payload, dict):
        return payload
    try:
        loaded = json.loads(str(payload))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


__all__ = [
    "build_nc_missing_doc_triage_report",
    "execute_top_nc_missing_doc_triage_action",
]
