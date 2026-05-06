from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.db.reprocess import (
    complete_historical_reprocess,
    enqueue_specific_historical_documents,
    latest_processing_run_for_document,
)
from duke_rates.historical.ncuc.downloader import NcucDownloader
from duke_rates.historical.ncuc.importer import NcucPipelineImporter
from duke_rates.historical.ncuc.missing_clean_doc_search import search_nc_missing_clean_documents
from duke_rates.historical.ncuc.missing_doc_status import build_nc_missing_doc_status_report
from duke_rates.historical.ncuc.pipeline.bulk_extractor import BulkExtractor
from duke_rates.models.ncuc import NcucDiscoveryRecord, NcucFetchStatus
from duke_rates.models.tariff import TariffVersionRecord

WORKFLOW_STAGES = [
    "search",
    "fetch",
    "import",
    "bootstrap_versions",
    "queue_reprocess",
    "process_reprocess",
    "validate",
]
_STAGE_ORDER = {name: idx for idx, name in enumerate(WORKFLOW_STAGES)}

PROMOTION_SCOPES = {
    "search_hits": ("fetch", "queue_reprocess"),
    "imported_docs": ("queue_reprocess", "queue_reprocess"),
}


def run_nc_missing_doc_workflow(
    settings: Settings,
    repository: Repository,
    *,
    from_stage: str = "search",
    to_stage: str = "queue_reprocess",
    family_key: str | None = None,
    discovery_record_ids: list[int] | None = None,
    historical_document_ids: list[int] | None = None,
    limit: int = 20,
    min_priority: str = "medium",
    structured_max_results: int = 50,
    keyword_max_results: int = 20,
    max_candidates_per_family: int = 12,
    persist_search: bool = True,
    save_manifest: bool = True,
    auto_promote_search_hits: bool = True,
    promotion_min_ideality: str = "probable",
    promotion_min_confidence: float = 45.0,
    auto_promote_imported_docs: bool = True,
    import_promotion_min_family_score: float = 24.0,
    fetch_retry_failed: bool = False,
    reprocess_priority: int = 85,
    requested_by: str = "workflow",
) -> dict[str, Any]:
    _validate_stage_window(from_stage, to_stage)

    report: dict[str, Any] = {
        "from_stage": from_stage,
        "to_stage": to_stage,
        "family_key": family_key,
        "stages": {},
        "discovery_record_ids": list(discovery_record_ids or []),
        "historical_document_ids": list(historical_document_ids or []),
    }

    current_discovery_ids = list(discovery_record_ids or [])
    current_historical_ids = list(historical_document_ids or [])

    if _stage_enabled("search", from_stage, to_stage):
        search_report = search_nc_missing_clean_documents(
            settings,
            repository,
            limit=limit,
            min_priority=min_priority,
            family_key=family_key,
            structured_max_results=structured_max_results,
            keyword_max_results=keyword_max_results,
            max_candidates_per_family=max_candidates_per_family,
            persist=persist_search,
            save_manifest=save_manifest,
        )
        report["stages"]["search"] = search_report
        current_discovery_ids.extend(
            discovery_id
            for row in search_report.get("rows", [])
            for discovery_id in row.get("persisted_discovery_ids", [])
        )
        current_discovery_ids = _dedupe_ints(current_discovery_ids)

    if _stage_enabled("fetch", from_stage, to_stage):
        fetch_report = _fetch_discovery_records(
            settings,
            repository,
            family_key=family_key,
            discovery_record_ids=current_discovery_ids,
            limit=limit,
        retry_failed=fetch_retry_failed,
        auto_promote_search_hits=auto_promote_search_hits,
        promotion_min_ideality=promotion_min_ideality,
        promotion_min_confidence=promotion_min_confidence,
    )
        report["stages"]["fetch"] = fetch_report
        current_discovery_ids = _dedupe_ints(fetch_report["record_ids"])

    if _stage_enabled("import", from_stage, to_stage):
        import_report = _import_discovery_records(
            settings,
            repository,
            family_key=family_key,
            discovery_record_ids=current_discovery_ids,
            limit=limit,
        )
        report["stages"]["import"] = import_report
        current_historical_ids.extend(import_report["historical_document_ids"])
        current_historical_ids = _dedupe_ints(current_historical_ids)
        current_discovery_ids = _dedupe_ints(
            import_report["record_ids"] or current_discovery_ids
        )

    if _stage_enabled("bootstrap_versions", from_stage, to_stage):
        bootstrap_report = _bootstrap_historical_versions(
            repository,
            family_key=family_key,
            historical_document_ids=current_historical_ids,
            limit=limit,
        )
        report["stages"]["bootstrap_versions"] = bootstrap_report
        current_historical_ids = _dedupe_ints(
            bootstrap_report["historical_document_ids"] or current_historical_ids
        )

    if _stage_enabled("queue_reprocess", from_stage, to_stage):
        queue_report = _queue_historical_documents_for_reprocess(
            repository,
            family_key=family_key,
            historical_document_ids=current_historical_ids,
            limit=limit,
            priority=reprocess_priority,
            requested_by=requested_by,
            auto_promote_imported_docs=auto_promote_imported_docs,
            import_promotion_min_family_score=import_promotion_min_family_score,
        )
        report["stages"]["queue_reprocess"] = queue_report
        current_historical_ids = _dedupe_ints(
            queue_report["historical_document_ids"] or current_historical_ids
        )

    if _stage_enabled("process_reprocess", from_stage, to_stage):
        process_report = _process_reprocess_stage(
            settings,
            repository,
            historical_document_ids=current_historical_ids,
            limit=limit,
        )
        report["stages"]["process_reprocess"] = process_report
        current_historical_ids = _dedupe_ints(
            process_report["historical_document_ids"] or current_historical_ids
        )

    if _stage_enabled("validate", from_stage, to_stage):
        validate_report = _validate_missing_doc_targets(
            settings,
            repository,
            family_key=family_key,
            discovery_record_ids=current_discovery_ids,
            historical_document_ids=current_historical_ids,
        )
        report["stages"]["validate"] = validate_report

    report["discovery_record_ids"] = current_discovery_ids
    report["historical_document_ids"] = current_historical_ids
    return report


def promote_nc_missing_doc_targets(
    settings: Settings,
    repository: Repository,
    *,
    scope: str = "search_hits",
    family_key: str | None = None,
    discovery_record_ids: list[int] | None = None,
    historical_document_ids: list[int] | None = None,
    limit: int = 20,
    auto_promote_search_hits: bool = True,
    promotion_min_ideality: str = "probable",
    promotion_min_confidence: float = 45.0,
    auto_promote_imported_docs: bool = True,
    import_promotion_min_family_score: float = 24.0,
    fetch_retry_failed: bool = False,
    reprocess_priority: int = 85,
    requested_by: str = "workflow",
) -> dict[str, Any]:
    if scope not in PROMOTION_SCOPES:
        raise ValueError(f"Unknown promotion scope: {scope}")
    from_stage, to_stage = PROMOTION_SCOPES[scope]
    return run_nc_missing_doc_workflow(
        settings,
        repository,
        from_stage=from_stage,
        to_stage=to_stage,
        family_key=family_key,
        discovery_record_ids=discovery_record_ids,
        historical_document_ids=historical_document_ids,
        limit=limit,
        persist_search=False,
        save_manifest=False,
        auto_promote_search_hits=auto_promote_search_hits,
        promotion_min_ideality=promotion_min_ideality,
        promotion_min_confidence=promotion_min_confidence,
        auto_promote_imported_docs=auto_promote_imported_docs,
        import_promotion_min_family_score=import_promotion_min_family_score,
        fetch_retry_failed=fetch_retry_failed,
        reprocess_priority=reprocess_priority,
        requested_by=requested_by,
    )


def _fetch_discovery_records(
    settings: Settings,
    repository: Repository,
    *,
    family_key: str | None,
    discovery_record_ids: list[int],
    limit: int,
    retry_failed: bool,
    auto_promote_search_hits: bool,
    promotion_min_ideality: str,
    promotion_min_confidence: float,
) -> dict[str, Any]:
    selected = _resolve_discovery_records_for_fetch(
        repository,
        family_key=family_key,
        discovery_record_ids=discovery_record_ids,
        limit=limit,
        retry_failed=retry_failed,
    )
    promotable_ids: list[int] = []
    deferred_ids: list[int] = []
    deferred_reasons: dict[int, list[str]] = {}
    if auto_promote_search_hits:
        promoted_selected: list[NcucDiscoveryRecord] = []
        for record in selected:
            is_promotable, reasons = _should_promote_discovery_record(
                record,
                min_ideality=promotion_min_ideality,
                min_confidence=promotion_min_confidence,
            )
            if is_promotable:
                promoted_selected.append(record)
                if record.id is not None:
                    promotable_ids.append(int(record.id))
            else:
                if record.id is not None:
                    deferred_ids.append(int(record.id))
                    deferred_reasons[int(record.id)] = reasons
        selected = promoted_selected
    _persist_search_promotion_decisions(
        repository,
        promoted_ids=promotable_ids,
        deferred_reasons=deferred_reasons,
        min_ideality=promotion_min_ideality,
        min_confidence=promotion_min_confidence,
    )
    if not selected:
        return {
            "record_ids": [],
            "selected_count": 0,
            "promoted_record_ids": promotable_ids,
            "deferred_record_ids": deferred_ids,
            "deferred_reasons": deferred_reasons,
            "fetched_count": 0,
            "success_count": 0,
            "success_record_ids": [],
        }
    downloader = NcucDownloader(settings, repository)
    try:
        results = [downloader.fetch(record) for record in selected]
    finally:
        downloader.close()
    success_ids = [int(record.id) for record in results if record.id and record.fetch_status == NcucFetchStatus.SUCCESS]
    return {
        "record_ids": [int(record.id) for record in results if record.id],
        "selected_count": len(selected),
        "promoted_record_ids": promotable_ids,
        "deferred_record_ids": deferred_ids,
        "deferred_reasons": deferred_reasons,
        "fetched_count": len(results),
        "success_count": len(success_ids),
        "success_record_ids": success_ids,
    }


def _import_discovery_records(
    settings: Settings,
    repository: Repository,
    *,
    family_key: str | None,
    discovery_record_ids: list[int],
    limit: int,
) -> dict[str, Any]:
    importer = NcucPipelineImporter(settings, repository)
    records = _resolve_discovery_records_for_import(
        repository,
        family_key=family_key,
        discovery_record_ids=discovery_record_ids,
        limit=limit,
    )
    summaries = [importer.import_discovery_record(record) for record in records]
    mined_historical_ids: list[int] = []
    mine_spans = getattr(importer, "mine_discovery_record_spans", None)
    for record, summary in zip(records, summaries, strict=False):
        span_ids = mine_spans(record) if callable(mine_spans) else []
        if span_ids:
            summary["historical_document_ids"] = _dedupe_ints(
                [*summary.get("historical_document_ids", []), *span_ids]
            )
            mined_historical_ids.extend(span_ids)
    historical_ids = _dedupe_ints(
        historical_id
        for summary in summaries
        for historical_id in summary.get("historical_document_ids", [])
    )
    return {
        "record_ids": [int(record.id) for record in records if record.id],
        "imported_count": len(records),
        "historical_document_ids": historical_ids,
        "mined_historical_document_ids": _dedupe_ints(mined_historical_ids),
        "family_keys_matched": sorted(
            {
                family
                for summary in summaries
                for family in summary.get("family_keys_matched", [])
                if family
            }
        ),
    }


def _bootstrap_historical_versions(
    repository: Repository,
    *,
    family_key: str | None,
    historical_document_ids: list[int],
    limit: int,
) -> dict[str, Any]:
    docs = _resolve_historical_documents(
        repository,
        family_key=family_key,
        historical_document_ids=historical_document_ids,
        limit=limit,
    )
    created = 0
    reused = 0
    bootstrapped_ids: list[int] = []
    for doc in docs:
        doc_id = int(doc.id or 0)
        if not doc_id or not doc.family_key:
            continue
        before = {
            int(version.id)
            for version in repository.list_tariff_versions(doc.family_key)
            if version.id is not None and version.historical_document_id == doc_id
        }
        _ensure_historical_tariff_version(
            repository,
            historical_document_id=doc_id,
            family_key=doc.family_key,
            effective_start=doc.effective_start,
        )
        after = {
            int(version.id)
            for version in repository.list_tariff_versions(doc.family_key)
            if version.id is not None and version.historical_document_id == doc_id
        }
        if before:
            reused += 1
        elif after:
            created += 1
        bootstrapped_ids.append(doc_id)
    return {
        "historical_document_ids": _dedupe_ints(bootstrapped_ids),
        "selected_count": len(docs),
        "created_count": created,
        "reused_count": reused,
    }


def _queue_historical_documents_for_reprocess(
    repository: Repository,
    *,
    family_key: str | None,
    historical_document_ids: list[int],
    limit: int,
    priority: int,
    requested_by: str,
    auto_promote_imported_docs: bool,
    import_promotion_min_family_score: float,
) -> dict[str, Any]:
    docs = _resolve_historical_documents(
        repository,
        family_key=family_key,
        historical_document_ids=historical_document_ids,
        limit=limit,
    )
    promotable_ids: list[int] = []
    deferred_ids: list[int] = []
    deferred_reasons: dict[int, list[str]] = {}
    queue_metadata_by_id: dict[int, dict[str, Any]] = {}

    for doc in docs:
        doc_id = getattr(doc, "id", None)
        if doc_id is None:
            continue
        doc_id = int(doc_id)
        if auto_promote_imported_docs:
            is_promotable, reasons, metadata = _should_promote_historical_document(
                doc,
                min_family_score=import_promotion_min_family_score,
            )
            if is_promotable:
                promotable_ids.append(doc_id)
                queue_metadata_by_id[doc_id] = metadata
            else:
                deferred_ids.append(doc_id)
                deferred_reasons[doc_id] = reasons
        elif getattr(doc, "local_path", None):
            promotable_ids.append(doc_id)
            queue_metadata_by_id[doc_id] = _historical_promotion_metadata(doc)
        else:
            deferred_ids.append(doc_id)
            deferred_reasons[doc_id] = ["no_local_path"]
    _persist_import_promotion_decisions(
        repository,
        promoted_metadata=queue_metadata_by_id,
        deferred_reasons=deferred_reasons,
        min_family_score=import_promotion_min_family_score,
    )
    doc_ids = _dedupe_ints(promotable_ids)
    if not doc_ids:
        return {
            "historical_document_ids": _dedupe_ints(
                doc.id for doc in docs if getattr(doc, "id", None) is not None
            ),
            "selected_count": len(docs),
            "promoted_historical_document_ids": [],
            "deferred_historical_document_ids": deferred_ids,
            "deferred_reasons": deferred_reasons,
            "inserted": 0,
            "skipped": 0,
            "queue_ids": [],
            "missing_ids": [],
        }
    with repository._connect() as conn:
        report = enqueue_specific_historical_documents(
            conn,
            historical_document_ids=doc_ids,
            priority=priority,
            requested_by=requested_by,
            queue_reason="missing_doc_workflow",
            metadata_by_id=queue_metadata_by_id,
        )
        conn.commit()
    report["historical_document_ids"] = _dedupe_ints(
        doc.id for doc in docs if getattr(doc, "id", None) is not None
    )
    report["selected_count"] = len(docs)
    report["promoted_historical_document_ids"] = doc_ids
    report["deferred_historical_document_ids"] = deferred_ids
    report["deferred_reasons"] = deferred_reasons
    return report


def _process_reprocess_stage(
    settings: Settings,
    repository: Repository,
    *,
    historical_document_ids: list[int],
    limit: int,
) -> dict[str, Any]:
    extractor = BulkExtractor(str(settings.database_path))
    selected_ids = _dedupe_ints(historical_document_ids)
    selected_id_set = set(selected_ids)
    processed = 0
    completed = 0
    failed = 0
    completed_ids: list[int] = []
    failed_ids: list[int] = []
    queue_ids: list[int] = []
    latest_run_ids: list[int] = []

    while processed < limit:
        item = _claim_next_targeted_historical_reprocess(
            repository,
            historical_document_ids=selected_ids,
        )
        if not item:
            break

        queue_id = int(item["id"])
        historical_document_id = int(item["historical_document_id"])
        if selected_id_set and historical_document_id not in selected_id_set:
            continue

        queue_ids.append(queue_id)
        doc = extractor.get_document_for_extraction(historical_document_id)
        if not doc:
            with repository._connect() as conn:
                complete_historical_reprocess(
                    conn,
                    queue_id=queue_id,
                    status="failed",
                    error_message=f"Historical document {historical_document_id} not found.",
                )
                conn.commit()
            processed += 1
            failed += 1
            failed_ids.append(historical_document_id)
            continue

        version_bootstrapped = False
        version_id = extractor.get_tariff_version_for_document(historical_document_id)
        if version_id is None:
            family_key = doc.get("family_key")
            if not family_key:
                with repository._connect() as conn:
                    complete_historical_reprocess(
                        conn,
                        queue_id=queue_id,
                        status="failed",
                        error_message=f"Historical document {historical_document_id} has no family_key.",
                    )
                    conn.commit()
                processed += 1
                failed += 1
                failed_ids.append(historical_document_id)
                continue
            version_id = _ensure_historical_tariff_version(
                repository,
                historical_document_id=historical_document_id,
                family_key=str(family_key),
                effective_start=doc.get("effective_start"),
            )
            doc["version_id"] = version_id
            version_bootstrapped = True

        try:
            _, family_key, inserted = extractor.process_document(doc)
            with repository._connect() as conn:
                latest_run = latest_processing_run_for_document(
                    conn,
                    historical_document_id=historical_document_id,
                )
                complete_historical_reprocess(
                    conn,
                    queue_id=queue_id,
                    status="completed",
                    latest_run_id=latest_run["id"] if latest_run else None,
                    metadata={
                        "charges_inserted": inserted,
                        "family_key": family_key,
                        "version_bootstrapped": version_bootstrapped,
                        "version_id": doc.get("version_id") or version_id,
                    },
                )
                conn.commit()
            processed += 1
            completed += 1
            completed_ids.append(historical_document_id)
            if latest_run and latest_run.get("id") is not None:
                latest_run_ids.append(int(latest_run["id"]))
        except Exception as exc:
            with repository._connect() as conn:
                complete_historical_reprocess(
                    conn,
                    queue_id=queue_id,
                    status="failed",
                    error_message=str(exc),
                )
                conn.commit()
            processed += 1
            failed += 1
            failed_ids.append(historical_document_id)

    return {
        "historical_document_ids": selected_ids,
        "processed": processed,
        "completed": completed,
        "failed": failed,
        "completed_historical_document_ids": _dedupe_ints(completed_ids),
        "failed_historical_document_ids": _dedupe_ints(failed_ids),
        "queue_ids": _dedupe_ints(queue_ids),
        "latest_run_ids": _dedupe_ints(latest_run_ids),
    }


def _claim_next_targeted_historical_reprocess(
    repository: Repository,
    *,
    historical_document_ids: list[int],
) -> dict[str, Any] | None:
    with repository._connect() as conn:
        query = """
            SELECT *
            FROM historical_reprocess_queue
            WHERE status = 'pending'
        """
        params: list[Any] = []
        if historical_document_ids:
            placeholders = ",".join("?" for _ in historical_document_ids)
            query += f" AND historical_document_id IN ({placeholders})"
            params.extend(int(item) for item in historical_document_ids)
        query += " ORDER BY priority DESC, requested_at ASC LIMIT 1"
        row = conn.execute(query, tuple(params)).fetchone()
        if not row:
            conn.commit()
            return None
        conn.execute(
            """
            UPDATE historical_reprocess_queue
            SET status = 'running', started_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (int(row["id"]),),
        )
        claimed = conn.execute(
            "SELECT * FROM historical_reprocess_queue WHERE id = ?",
            (int(row["id"]),),
        ).fetchone()
        conn.commit()
    return dict(claimed) if claimed else None


def _validate_missing_doc_targets(
    settings: Settings,
    repository: Repository,
    *,
    family_key: str | None,
    discovery_record_ids: list[int],
    historical_document_ids: list[int],
) -> dict[str, Any]:
    target_family_key = family_key
    status_reports: list[dict[str, Any]] = []

    if target_family_key:
        status_reports.append(
            build_nc_missing_doc_status_report(repository, family_key=target_family_key)
        )

    for discovery_record_id in _dedupe_ints(discovery_record_ids):
        status_reports.append(
            build_nc_missing_doc_status_report(
                repository,
                discovery_record_id=discovery_record_id,
            )
        )

    for historical_document_id in _dedupe_ints(historical_document_ids):
        status_reports.append(
            build_nc_missing_doc_status_report(
                repository,
                historical_document_id=historical_document_id,
            )
        )

    _persist_target_triage_decisions(repository, status_reports=status_reports)

    target_summaries: list[dict[str, Any]] = []
    next_action_counts: dict[str, int] = {}
    blocked_reason_counts: dict[str, int] = {}
    total_needs_review = 0
    total_queued = 0
    total_strong = 0
    total_weak_or_empty = 0

    for report in status_reports:
        target = report.get("target") or {}
        summary = report.get("summary") or {}
        discovery_actions = [
            {
                "id": row.get("id"),
                "next_action": row.get("next_action"),
                "blocked_reason": row.get("blocked_reason"),
            }
            for row in report.get("discovery_records", [])
        ]
        historical_actions = [
            {
                "id": row.get("id"),
                "next_action": row.get("next_action"),
                "blocked_reason": row.get("blocked_reason"),
                "current_stage": row.get("current_stage"),
            }
            for row in report.get("historical_documents", [])
        ]
        for row in discovery_actions + historical_actions:
            action = str(row.get("next_action") or "unknown")
            next_action_counts[action] = next_action_counts.get(action, 0) + 1
            blocked = str(row.get("blocked_reason") or "").strip()
            if blocked:
                blocked_reason_counts[blocked] = blocked_reason_counts.get(blocked, 0) + 1
        total_needs_review += int(summary.get("needs_review_count") or 0)
        total_queued += int(summary.get("queued_reprocess_count") or 0)
        for row in report.get("historical_documents", []):
            latest_run = row.get("latest_processing_run") or {}
            quality = str(latest_run.get("outcome_quality") or "").lower()
            if quality == "strong":
                total_strong += 1
            elif quality in {"weak", "empty"}:
                total_weak_or_empty += 1
        target_summaries.append(
            {
                "target": target,
                "summary": summary,
                "discovery_actions": discovery_actions,
                "historical_actions": historical_actions,
            }
        )

    return {
        "family_key": target_family_key,
        "discovery_record_ids": _dedupe_ints(discovery_record_ids),
        "historical_document_ids": _dedupe_ints(historical_document_ids),
        "target_count": len(status_reports),
        "targets": status_reports,
        "target_summaries": target_summaries,
        "triage_summary": {
            "next_action_counts": dict(sorted(next_action_counts.items())),
            "blocked_reason_counts": dict(sorted(blocked_reason_counts.items())),
            "needs_review_count": total_needs_review,
            "queued_reprocess_count": total_queued,
            "strong_processed_count": total_strong,
            "weak_or_empty_processed_count": total_weak_or_empty,
        },
    }


def _persist_target_triage_decisions(
    repository: Repository,
    *,
    status_reports: list[dict[str, Any]],
) -> None:
    updated_at = datetime.now(UTC).isoformat()
    for report in status_reports:
        target = report.get("target") or {}
        family_key = target.get("family_key")

        for row in report.get("discovery_records", []):
            record_id = row.get("id")
            if record_id is None:
                continue
            record = repository.get_ncuc_discovery_record(int(record_id))
            if not record:
                continue
            metadata = _loads_json_object(getattr(record, "metadata_json", None))
            workflow_meta = _loads_json_object(metadata.get("missing_doc_workflow"))
            workflow_meta["triage"] = {
                "scope": "discovery_record",
                "family_key": family_key,
                "next_action": row.get("next_action"),
                "blocked_reason": row.get("blocked_reason"),
                "fetch_status": row.get("fetch_status"),
                "linked_historical_document_ids": list(row.get("linked_historical_document_ids") or []),
                "search_promotion_assessment": dict(row.get("search_promotion_assessment") or {}),
                "updated_at": updated_at,
            }
            metadata["missing_doc_workflow"] = workflow_meta
            _save_discovery_metadata(repository, record, metadata)

        for row in report.get("historical_documents", []):
            historical_document_id = row.get("id")
            if historical_document_id is None:
                continue
            doc = repository.get_historical_document(int(historical_document_id))
            if not doc:
                continue
            metadata = _loads_json_object(getattr(doc, "metadata_json", None))
            workflow_meta = _loads_json_object(metadata.get("missing_doc_workflow"))
            workflow_meta["triage"] = {
                "scope": "historical_document",
                "family_key": family_key or row.get("family_key"),
                "next_action": row.get("next_action"),
                "blocked_reason": row.get("blocked_reason"),
                "current_stage": row.get("current_stage"),
                "latest_run_status": (row.get("latest_processing_run") or {}).get("status"),
                "latest_outcome_quality": (row.get("latest_processing_run") or {}).get("outcome_quality"),
                "latest_review_outcome": (row.get("latest_review") or {}).get("outcome"),
                "latest_queue_status": (row.get("latest_reprocess_queue") or {}).get("status"),
                "import_promotion_assessment": dict(row.get("import_promotion_assessment") or {}),
                "updated_at": updated_at,
            }
            metadata["missing_doc_workflow"] = workflow_meta
            _save_historical_metadata(repository, int(historical_document_id), metadata)


def _resolve_discovery_records_for_fetch(
    repository: Repository,
    *,
    family_key: str | None,
    discovery_record_ids: list[int],
    limit: int,
    retry_failed: bool,
) -> list[NcucDiscoveryRecord]:
    if discovery_record_ids:
        statuses = {
            NcucFetchStatus.PENDING.value,
            NcucFetchStatus.REQUIRES_BROWSER.value,
        }
        if retry_failed:
            statuses.add(NcucFetchStatus.FAILED.value)
        records = []
        for record_id in discovery_record_ids:
            record = repository.get_ncuc_discovery_record(int(record_id))
            if record and record.fetch_status in statuses:
                records.append(record)
        return records[:limit]

    statuses = [NcucFetchStatus.PENDING.value, NcucFetchStatus.REQUIRES_BROWSER.value]
    if retry_failed:
        statuses.append(NcucFetchStatus.FAILED.value)
    records: list[NcucDiscoveryRecord] = []
    for status in statuses:
        records.extend(repository.list_ncuc_discovery_records(fetch_status=status, family_key=family_key))
    return records[:limit]


def _resolve_discovery_records_for_import(
    repository: Repository,
    *,
    family_key: str | None,
    discovery_record_ids: list[int],
    limit: int,
) -> list[NcucDiscoveryRecord]:
    if discovery_record_ids:
        records = []
        for record_id in discovery_record_ids:
            record = repository.get_ncuc_discovery_record(int(record_id))
            if record and record.fetch_status in {NcucFetchStatus.SUCCESS, NcucFetchStatus.SKIPPED_DUPLICATE}:
                records.append(record)
        return records[:limit]
    records = repository.list_ncuc_discovery_records(fetch_status=NcucFetchStatus.SUCCESS.value, family_key=family_key)
    return records[:limit]


def _resolve_historical_documents(
    repository: Repository,
    *,
    family_key: str | None,
    historical_document_ids: list[int],
    limit: int,
):
    docs = repository.list_historical_documents(state="NC")
    if family_key:
        docs = [doc for doc in docs if doc.family_key == family_key]
    if historical_document_ids:
        wanted = {int(item) for item in historical_document_ids}
        docs = [doc for doc in docs if doc.id is not None and int(doc.id) in wanted]
    return docs[:limit] if limit else docs


def _ensure_historical_tariff_version(
    repository: Repository,
    *,
    historical_document_id: int,
    family_key: str,
    effective_start: str | None,
    ) -> int:
    for version in repository.list_tariff_versions(family_key):
        if version.historical_document_id == historical_document_id:
            if version.id is None:
                raise ValueError(
                    f"Existing tariff_version for historical document {historical_document_id} is missing an id."
                )
            return int(version.id)
    return repository.upsert_tariff_version(
        TariffVersionRecord(
            family_key=family_key,
            historical_document_id=historical_document_id,
            effective_start=effective_start,
            source_type="regulator",
            confidence_score=0.5,
            notes="Bootstrapped for missing document workflow.",
        )
    )


def _should_promote_discovery_record(
    record: NcucDiscoveryRecord,
    *,
    min_ideality: str,
    min_confidence: float,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    ideality_rank = {"skip": 0, "possible": 1, "probable": 2, "ideal": 3}
    current_ideality = str(getattr(record, "search_ideality", None) or "possible").lower()
    current_rank = ideality_rank.get(current_ideality, 1)
    required_rank = ideality_rank.get(str(min_ideality or "probable").lower(), 2)
    confidence = float(getattr(record, "search_confidence_score", None) or 0.0)
    if current_rank < required_rank:
        reasons.append(f"ideality_below_threshold:{current_ideality}")
    if confidence < float(min_confidence):
        reasons.append(f"confidence_below_threshold:{confidence:.2f}")
    if not (
        getattr(record, "download_url", None)
        or getattr(record, "viewer_url", None)
        or getattr(record, "attachment_url", None)
    ):
        reasons.append("no_downloadable_url")
    metadata = _loads_json_object(getattr(record, "metadata_json", None))
    audit_context = metadata.get("audit_context") if isinstance(metadata, dict) else {}
    if isinstance(audit_context, dict):
        priority_band = str(audit_context.get("priority_band") or "").lower()
        if priority_band == "high" and current_rank >= ideality_rank["possible"] and confidence >= max(20.0, min_confidence - 10.0):
            reasons = [reason for reason in reasons if not reason.startswith("confidence_below_threshold:")]
    return (not reasons), reasons


def _should_promote_historical_document(
    doc,
    *,
    min_family_score: float,
) -> tuple[bool, list[str], dict[str, Any]]:
    reasons: list[str] = []
    metadata = _historical_promotion_metadata(doc)
    family_key = str(getattr(doc, "family_key", None) or "").strip()
    local_path = getattr(doc, "local_path", None)
    family_score = float(metadata.get("family_match_score") or 0.0)
    effective_start = str(getattr(doc, "effective_start", None) or "").strip()
    if not family_key:
        reasons.append("no_family_key")
    if not local_path:
        reasons.append("no_local_path")
    if _looks_like_provisional_family_key(family_key):
        reasons.append("provisional_family_key")
    if family_score < float(min_family_score):
        reasons.append(f"family_match_below_threshold:{family_score:.2f}")
    if not effective_start and family_score < max(35.0, float(min_family_score) + 8.0):
        reasons.append("missing_effective_start_for_weak_match")
    return (not reasons), reasons, metadata


def _historical_promotion_metadata(doc) -> dict[str, Any]:
    evidence = _loads_json_object(getattr(doc, "evidence_json", None))
    return {
        "workflow_stage": "queue_reprocess",
        "promotion_basis": "historical_import",
        "family_match_score": _historical_family_match_score(doc),
        "effective_start": getattr(doc, "effective_start", None),
        "start_page": getattr(doc, "start_page", None),
        "end_page": getattr(doc, "end_page", None),
        "family_key": getattr(doc, "family_key", None),
        "evidence_breakdown": evidence,
    }


def _historical_family_match_score(doc) -> float:
    evidence = _loads_json_object(getattr(doc, "evidence_json", None))
    score = 0.0
    for value in evidence.values():
        try:
            score += float(value or 0.0)
        except (TypeError, ValueError):
            continue
    return round(score, 4)


def _looks_like_provisional_family_key(family_key: str | None) -> bool:
    normalized = str(family_key or "").strip().lower()
    return "-doc-" in normalized or normalized.endswith("-doc")


def _persist_search_promotion_decisions(
    repository: Repository,
    *,
    promoted_ids: list[int],
    deferred_reasons: dict[int, list[str]],
    min_ideality: str,
    min_confidence: float,
) -> None:
    wanted_ids = _dedupe_ints([*promoted_ids, *deferred_reasons.keys()])
    for record_id in wanted_ids:
        record = repository.get_ncuc_discovery_record(int(record_id))
        if not record:
            continue
        metadata = _loads_json_object(getattr(record, "metadata_json", None))
        workflow_meta = _loads_json_object(metadata.get("missing_doc_workflow"))
        workflow_meta["search_promotion"] = {
            "promotable": int(record_id) in set(promoted_ids),
            "reasons": list(deferred_reasons.get(int(record_id), [])),
            "search_confidence_score": float(getattr(record, "search_confidence_score", None) or 0.0),
            "search_ideality": str(getattr(record, "search_ideality", None) or "possible").lower(),
            "thresholds": {
                "min_ideality": min_ideality,
                "min_confidence": float(min_confidence),
            },
        }
        metadata["missing_doc_workflow"] = workflow_meta
        _save_discovery_metadata(repository, record, metadata)


def _persist_import_promotion_decisions(
    repository: Repository,
    *,
    promoted_metadata: dict[int, dict[str, Any]],
    deferred_reasons: dict[int, list[str]],
    min_family_score: float,
) -> None:
    wanted_ids = _dedupe_ints([*promoted_metadata.keys(), *deferred_reasons.keys()])
    for historical_document_id in wanted_ids:
        doc = repository.get_historical_document(int(historical_document_id))
        if not doc:
            continue
        metadata = _loads_json_object(getattr(doc, "metadata_json", None))
        workflow_meta = _loads_json_object(metadata.get("missing_doc_workflow"))
        assessment = dict(promoted_metadata.get(int(historical_document_id), _historical_promotion_metadata(doc)))
        assessment["promotable"] = int(historical_document_id) in set(promoted_metadata)
        assessment["reasons"] = list(deferred_reasons.get(int(historical_document_id), []))
        assessment["thresholds"] = {
            "min_family_score": float(min_family_score),
        }
        workflow_meta["import_promotion"] = assessment
        metadata["missing_doc_workflow"] = workflow_meta
        _save_historical_metadata(repository, int(historical_document_id), metadata)


def _save_discovery_metadata(
    repository: Repository,
    record,
    metadata: dict[str, Any],
) -> None:
    metadata_json = json.dumps(metadata, sort_keys=True)
    try:
        setattr(record, "metadata_json", metadata_json)
    except Exception:
        pass
    if hasattr(record, "model_copy") and hasattr(repository, "upsert_ncuc_discovery_record"):
        updated = record.model_copy(update={"metadata_json": metadata_json})
        repository.upsert_ncuc_discovery_record(updated)
        return
    try:
        with repository._connect() as conn:
            conn.execute(
                "UPDATE ncuc_discovery_records SET metadata_json = ? WHERE id = ?",
                (metadata_json, int(record.id)),
            )
            conn.commit()
    except Exception:
        return


def _save_historical_metadata(
    repository: Repository,
    historical_document_id: int,
    metadata: dict[str, Any],
) -> None:
    metadata_json = json.dumps(metadata, sort_keys=True)
    current = None
    try:
        current = repository.get_historical_document(int(historical_document_id))
    except Exception:
        current = None
    if current is not None:
        try:
            setattr(current, "metadata_json", metadata_json)
        except Exception:
            pass
    try:
        with repository._connect() as conn:
            conn.execute(
                "UPDATE historical_documents SET metadata_json = ? WHERE id = ?",
                (metadata_json, int(historical_document_id)),
            )
            conn.commit()
    except Exception:
        return


def _loads_json_object(payload: Any) -> dict[str, Any]:
    if not payload:
        return {}
    if isinstance(payload, dict):
        return payload
    try:
        value = __import__("json").loads(str(payload))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _stage_enabled(stage_name: str, from_stage: str, to_stage: str) -> bool:
    return _STAGE_ORDER[from_stage] <= _STAGE_ORDER[stage_name] <= _STAGE_ORDER[to_stage]


def _validate_stage_window(from_stage: str, to_stage: str) -> None:
    if from_stage not in _STAGE_ORDER:
        raise ValueError(f"Unknown from_stage: {from_stage}")
    if to_stage not in _STAGE_ORDER:
        raise ValueError(f"Unknown to_stage: {to_stage}")
    if _STAGE_ORDER[from_stage] > _STAGE_ORDER[to_stage]:
        raise ValueError(f"from_stage {from_stage} comes after to_stage {to_stage}")


def _dedupe_ints(values) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for value in values:
        if value is None:
            continue
        item = int(value)
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


__all__ = [
    "PROMOTION_SCOPES",
    "WORKFLOW_STAGES",
    "promote_nc_missing_doc_targets",
    "run_nc_missing_doc_workflow",
]
