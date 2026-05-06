from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pdfplumber

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.historical.ncuc.document_param_search import (
    DocParamSearchResult,
    DocumentParamSearcher,
)
from duke_rates.historical.ncuc.missing_clean_doc_search import (
    search_nc_missing_clean_documents,
)
from duke_rates.historical.ncuc.pipeline.ocr_normalization import normalize_ocr_text
from duke_rates.historical.ncuc.session import (
    close_authenticated_context,
    create_authenticated_context,
)
from duke_rates.historical.ncuc.missing_doc_workflow import promote_nc_missing_doc_targets
from duke_rates.parse.heuristics import extract_effective_date

_DETAIL_PAGE_MARKERS = ("PSCDocumentDetailsPageNCUC", "DocumentId=")


def remediate_no_downloadable_url_discovery_records(
    settings: Settings,
    repository: Repository,
    *,
    family_key: str | None = None,
    discovery_record_ids: list[int] | None = None,
    limit: int = 20,
    delay_seconds: float = 0.2,
) -> dict[str, Any]:
    records = _select_no_downloadable_url_records(
        repository,
        family_key=family_key,
        discovery_record_ids=discovery_record_ids or [],
        limit=limit,
    )
    if not records:
        return {
            "selected_count": 0,
            "resolved_count": 0,
            "updated_record_ids": [],
            "unresolved_record_ids": [],
        }

    pw, ctx, page = create_authenticated_context(settings)
    try:
        searcher = DocumentParamSearcher(settings)
        resolved_count = 0
        updated_record_ids: list[int] = []
        unresolved_record_ids: list[int] = []

        for record in records:
            detail_url = _discovery_detail_url(record)
            if not detail_url:
                _persist_remediation_result(
                    repository,
                    record,
                    resolved=False,
                    reason="no_detail_url",
                    recovered_url=None,
                )
                if record.id is not None:
                    unresolved_record_ids.append(int(record.id))
                continue

            result = DocParamSearchResult(
                description=getattr(record, "filing_title", None) or "",
                doc_type="",
                date_filed=getattr(record, "filing_date", None) or "",
                docket_number=getattr(record, "docket_number", None) or "",
                docket_id="",
                company_name=getattr(record, "utility", None) or "",
                document_detail_url=detail_url,
            )
            enriched = searcher.enrich_with_document_details(
                page,
                [result],
                delay_seconds=delay_seconds,
            )[0]
            recovered_url = enriched.view_file_urls[0] if enriched.view_file_urls else None
            resolved = bool(recovered_url)
            _persist_remediation_result(
                repository,
                record,
                resolved=resolved,
                reason=None if resolved else "detail_page_no_viewfile",
                recovered_url=recovered_url,
                synopsis=enriched.synopsis,
            )
            if record.id is not None:
                if resolved:
                    resolved_count += 1
                    updated_record_ids.append(int(record.id))
                else:
                    unresolved_record_ids.append(int(record.id))
    finally:
        close_authenticated_context(pw, ctx)

    return {
        "selected_count": len(records),
        "resolved_count": resolved_count,
        "updated_record_ids": updated_record_ids,
        "unresolved_record_ids": unresolved_record_ids,
    }


def _select_no_downloadable_url_records(
    repository: Repository,
    *,
    family_key: str | None,
    discovery_record_ids: list[int],
    limit: int,
):
    if discovery_record_ids:
        rows = [
            repository.get_ncuc_discovery_record(int(record_id))
            for record_id in discovery_record_ids
        ]
        rows = [row for row in rows if row is not None]
    else:
        rows = repository.list_ncuc_discovery_records(family_key=family_key)
    filtered = []
    for row in rows:
        metadata = _loads_json_object(getattr(row, "metadata_json", None))
        workflow = _loads_json_object(metadata.get("missing_doc_workflow"))
        search_promotion = _loads_json_object(workflow.get("search_promotion"))
        reasons = [str(item) for item in search_promotion.get("reasons", [])]
        if "no_downloadable_url" not in reasons:
            continue
        filtered.append(row)
        if limit and len(filtered) >= limit:
            break
    return filtered


def remediate_missing_effective_start_historical_documents(
    repository: Repository,
    *,
    family_key: str | None = None,
    historical_document_ids: list[int] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    docs = _select_missing_effective_start_docs(
        repository,
        family_key=family_key,
        historical_document_ids=historical_document_ids or [],
        limit=limit,
    )
    resolved_count = 0
    updated_ids: list[int] = []
    unresolved_ids: list[int] = []

    for doc in docs:
        recovered = _recover_effective_start_for_doc(doc)
        if recovered:
            resolved_count += 1
            if doc.id is not None:
                updated_ids.append(int(doc.id))
            _persist_effective_start_remediation(
                repository,
                doc,
                effective_start=recovered,
                resolved=True,
                reason=None,
            )
        else:
            if doc.id is not None:
                unresolved_ids.append(int(doc.id))
            _persist_effective_start_remediation(
                repository,
                doc,
                effective_start=None,
                resolved=False,
                reason="effective_date_not_found_in_page_span",
            )

    return {
        "selected_count": len(docs),
        "resolved_count": resolved_count,
        "updated_historical_document_ids": updated_ids,
        "unresolved_historical_document_ids": unresolved_ids,
    }


def remediate_confidence_below_threshold_discovery_records(
    settings: Settings,
    repository: Repository,
    *,
    family_key: str | None = None,
    discovery_record_ids: list[int] | None = None,
    limit: int = 20,
    structured_max_results: int = 100,
    keyword_max_results: int = 40,
    max_candidates_per_family: int = 20,
) -> dict[str, Any]:
    records = _select_confidence_below_threshold_records(
        repository,
        family_key=family_key,
        discovery_record_ids=discovery_record_ids or [],
        limit=limit,
    )
    if not records:
        return {
            "selected_count": 0,
            "rerun_family_keys": [],
            "updated_record_ids": [],
            "unresolved_record_ids": [],
        }

    rerun_family_keys = sorted(
        {
            str(family).strip()
            for record in records
            for family in (getattr(record, "family_keys", None) or [])
            if str(family).strip()
        }
    )
    updated_record_ids: list[int] = []
    unresolved_record_ids: list[int] = []
    persisted_by_family: dict[str, list[int]] = {}

    for rerun_family_key in rerun_family_keys:
        search_report = search_nc_missing_clean_documents(
            settings,
            repository,
            limit=1,
            min_priority="low",
            family_key=rerun_family_key,
            structured_max_results=structured_max_results,
            keyword_max_results=keyword_max_results,
            max_candidates_per_family=max_candidates_per_family,
            enrich_portal_details=True,
            persist=True,
            save_manifest=True,
        )
        persisted_ids = [
            int(discovery_id)
            for row in search_report.get("rows", [])
            for discovery_id in row.get("persisted_discovery_ids", [])
        ]
        if persisted_ids:
            persisted_by_family[rerun_family_key] = persisted_ids
            updated_record_ids.extend(persisted_ids)

    updated_record_ids = _dedupe_ints(updated_record_ids)
    for record in records:
        related_ids = sorted(
            {
                persisted_id
                for family in (getattr(record, "family_keys", None) or [])
                for persisted_id in persisted_by_family.get(str(family).strip(), [])
            }
        )
        resolved = bool(related_ids)
        _persist_confidence_requery_result(
            repository,
            record,
            resolved=resolved,
            new_record_ids=related_ids,
        )
        if record.id is not None and not resolved:
            unresolved_record_ids.append(int(record.id))

    return {
        "selected_count": len(records),
        "rerun_family_keys": rerun_family_keys,
        "updated_record_ids": updated_record_ids,
        "unresolved_record_ids": unresolved_record_ids,
    }


def remediate_and_promote_missing_doc_targets(
    settings: Settings,
    repository: Repository,
    *,
    family_key: str | None = None,
    reasons: list[str] | None = None,
    limit: int = 20,
    delay_seconds: float = 0.2,
    promotion_min_ideality: str = "probable",
    promotion_min_confidence: float = 45.0,
    import_promotion_min_family_score: float = 24.0,
    requested_by: str = "workflow",
) -> dict[str, Any]:
    selected_reasons = _normalize_reason_selection(reasons)
    remediation_reports: dict[str, dict[str, Any]] = {}
    promotion_reports: dict[str, dict[str, Any]] = {}

    if "no_downloadable_url" in selected_reasons:
        remediation = remediate_no_downloadable_url_discovery_records(
            settings,
            repository,
            family_key=family_key,
            limit=limit,
            delay_seconds=delay_seconds,
        )
        remediation_reports["no_downloadable_url"] = remediation
        if remediation.get("updated_record_ids"):
            promotion_reports["no_downloadable_url"] = promote_nc_missing_doc_targets(
                settings,
                repository,
                scope="search_hits",
                family_key=family_key,
                discovery_record_ids=[int(item) for item in remediation.get("updated_record_ids", [])],
                limit=limit,
                auto_promote_search_hits=True,
                promotion_min_ideality=promotion_min_ideality,
                promotion_min_confidence=promotion_min_confidence,
                requested_by=requested_by,
            )

    if "missing_effective_start_for_weak_match" in selected_reasons:
        remediation = remediate_missing_effective_start_historical_documents(
            repository,
            family_key=family_key,
            limit=limit,
        )
        remediation_reports["missing_effective_start_for_weak_match"] = remediation
        if remediation.get("updated_historical_document_ids"):
            promotion_reports["missing_effective_start_for_weak_match"] = promote_nc_missing_doc_targets(
                settings,
                repository,
                scope="imported_docs",
                family_key=family_key,
                historical_document_ids=[int(item) for item in remediation.get("updated_historical_document_ids", [])],
                limit=limit,
                auto_promote_imported_docs=True,
                import_promotion_min_family_score=import_promotion_min_family_score,
                requested_by=requested_by,
            )

    if "confidence_below_threshold" in selected_reasons:
        remediation = remediate_confidence_below_threshold_discovery_records(
            settings,
            repository,
            family_key=family_key,
            limit=limit,
        )
        remediation_reports["confidence_below_threshold"] = remediation
        if remediation.get("updated_record_ids"):
            promotion_reports["confidence_below_threshold"] = promote_nc_missing_doc_targets(
                settings,
                repository,
                scope="search_hits",
                family_key=family_key,
                discovery_record_ids=[int(item) for item in remediation.get("updated_record_ids", [])],
                limit=limit,
                auto_promote_search_hits=True,
                promotion_min_ideality=promotion_min_ideality,
                promotion_min_confidence=promotion_min_confidence,
                requested_by=requested_by,
            )

    return {
        "family_key": family_key,
        "reasons": sorted(selected_reasons),
        "remediation_reports": remediation_reports,
        "promotion_reports": promotion_reports,
    }


def _select_missing_effective_start_docs(
    repository: Repository,
    *,
    family_key: str | None,
    historical_document_ids: list[int],
    limit: int,
):
    if historical_document_ids:
        rows = [
            repository.get_historical_document(int(historical_document_id))
            for historical_document_id in historical_document_ids
        ]
        rows = [row for row in rows if row is not None]
    else:
        rows = repository.list_historical_documents(state="NC")
        if family_key:
            rows = [row for row in rows if getattr(row, "family_key", None) == family_key]
    filtered = []
    for row in rows:
        metadata = _loads_json_object(getattr(row, "metadata_json", None))
        workflow = _loads_json_object(metadata.get("missing_doc_workflow"))
        import_promotion = _loads_json_object(workflow.get("import_promotion"))
        reasons = [str(item) for item in import_promotion.get("reasons", [])]
        if "missing_effective_start_for_weak_match" not in reasons:
            continue
        filtered.append(row)
        if limit and len(filtered) >= limit:
            break
    return filtered


def _select_confidence_below_threshold_records(
    repository: Repository,
    *,
    family_key: str | None,
    discovery_record_ids: list[int],
    limit: int,
):
    if discovery_record_ids:
        rows = [
            repository.get_ncuc_discovery_record(int(record_id))
            for record_id in discovery_record_ids
        ]
        rows = [row for row in rows if row is not None]
    else:
        rows = repository.list_ncuc_discovery_records(family_key=family_key)
    filtered = []
    for row in rows:
        metadata = _loads_json_object(getattr(row, "metadata_json", None))
        workflow = _loads_json_object(metadata.get("missing_doc_workflow"))
        search_promotion = _loads_json_object(workflow.get("search_promotion"))
        reasons = [str(item) for item in search_promotion.get("reasons", [])]
        if not any(reason.startswith("confidence_below_threshold:") for reason in reasons):
            continue
        filtered.append(row)
        if limit and len(filtered) >= limit:
            break
    return filtered


def _recover_effective_start_for_doc(doc) -> str | None:
    local_path = Path(str(getattr(doc, "local_path", "") or ""))
    start_page = getattr(doc, "start_page", None)
    end_page = getattr(doc, "end_page", None) or start_page
    if not local_path.exists() or start_page is None or end_page is None:
        return None
    text_parts: list[str] = []
    try:
        with pdfplumber.open(str(local_path)) as pdf:
            for page_number in range(int(start_page), int(end_page) + 1):
                page_index = page_number - 1
                if page_index < 0 or page_index >= len(pdf.pages):
                    continue
                text = pdf.pages[page_index].extract_text() or ""
                if text:
                    text_parts.append(normalize_ocr_text(text))
    except Exception:
        return None
    if not text_parts:
        return None
    return _normalize_effective_date(extract_effective_date("\n".join(text_parts)))


def _persist_effective_start_remediation(
    repository: Repository,
    doc,
    *,
    effective_start: str | None,
    resolved: bool,
    reason: str | None,
) -> None:
    metadata = _loads_json_object(getattr(doc, "metadata_json", None))
    workflow = _loads_json_object(metadata.get("missing_doc_workflow"))
    remediation = {
        "remediation_type": "missing_effective_start_for_weak_match",
        "resolved": resolved,
        "reason": reason,
        "recovered_effective_start": effective_start,
    }
    workflow["import_remediation"] = remediation

    import_promotion = _loads_json_object(workflow.get("import_promotion"))
    reasons = [str(item) for item in import_promotion.get("reasons", [])]
    if resolved and effective_start:
        reasons = [item for item in reasons if item != "missing_effective_start_for_weak_match"]
        import_promotion["promotable"] = not reasons
        import_promotion["effective_start"] = effective_start
    import_promotion["reasons"] = reasons
    workflow["import_promotion"] = import_promotion
    metadata["missing_doc_workflow"] = workflow
    _save_historical_document(repository, doc, effective_start=effective_start, metadata=metadata)


def _persist_remediation_result(
    repository: Repository,
    record,
    *,
    resolved: bool,
    reason: str | None,
    recovered_url: str | None,
    synopsis: str | None = None,
) -> None:
    metadata = _loads_json_object(getattr(record, "metadata_json", None))
    workflow = _loads_json_object(metadata.get("missing_doc_workflow"))
    remediation = {
        "remediation_type": "no_downloadable_url",
        "resolved": resolved,
        "reason": reason,
        "recovered_url": recovered_url,
    }
    if synopsis:
        remediation["synopsis"] = synopsis
    workflow["search_remediation"] = remediation

    search_promotion = _loads_json_object(workflow.get("search_promotion"))
    reasons = [str(item) for item in search_promotion.get("reasons", [])]
    if resolved:
        reasons = [item for item in reasons if item != "no_downloadable_url"]
        search_promotion["promotable"] = not reasons
    search_promotion["reasons"] = reasons
    workflow["search_promotion"] = search_promotion
    metadata["missing_doc_workflow"] = workflow

    update_payload = {
        "metadata_json": json.dumps(metadata, sort_keys=True),
    }
    if resolved and recovered_url:
        update_payload.update(
            {
                "viewer_url": recovered_url,
                "download_url": recovered_url,
                "attachment_url": recovered_url if recovered_url.lower().endswith(".pdf") else getattr(record, "attachment_url", None),
            }
        )
    updated = record.model_copy(update=update_payload)
    repository.upsert_ncuc_discovery_record(updated)


def _persist_confidence_requery_result(
    repository: Repository,
    record,
    *,
    resolved: bool,
    new_record_ids: list[int],
) -> None:
    metadata = _loads_json_object(getattr(record, "metadata_json", None))
    workflow = _loads_json_object(metadata.get("missing_doc_workflow"))
    remediation = {
        "remediation_type": "confidence_below_threshold",
        "resolved": resolved,
        "new_record_ids": [int(item) for item in new_record_ids],
    }
    workflow["search_requery_remediation"] = remediation
    metadata["missing_doc_workflow"] = workflow
    updated = record.model_copy(update={"metadata_json": json.dumps(metadata, sort_keys=True)})
    repository.upsert_ncuc_discovery_record(updated)


def _discovery_detail_url(record) -> str | None:
    for value in (
        getattr(record, "discovered_url", None),
        getattr(record, "viewer_url", None),
        getattr(record, "download_url", None),
    ):
        text = str(value or "").strip()
        if text and any(marker in text for marker in _DETAIL_PAGE_MARKERS):
            return text
    return None


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


def _normalize_reason_selection(reasons: list[str] | None) -> set[str]:
    allowed = {
        "confidence_below_threshold",
        "no_downloadable_url",
        "missing_effective_start_for_weak_match",
    }
    if not reasons:
        return set(allowed)
    normalized = {str(item).strip() for item in reasons if str(item).strip()}
    return {item for item in normalized if item in allowed}


def _dedupe_ints(values: list[int]) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for value in values:
        item = int(value)
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _save_historical_document(
    repository: Repository,
    doc,
    *,
    effective_start: str | None,
    metadata: dict[str, Any],
) -> None:
    metadata_json = json.dumps(metadata, sort_keys=True)
    current_effective_start = effective_start or getattr(doc, "effective_start", None)
    try:
        setattr(doc, "metadata_json", metadata_json)
        if effective_start:
            setattr(doc, "effective_start", effective_start)
    except Exception:
        pass
    if hasattr(doc, "model_copy"):
        updated = doc.model_copy(
            update={
                "effective_start": current_effective_start,
                "metadata_json": metadata_json,
            }
        )
        repository.upsert_historical_document(updated)
        return
    try:
        with repository._connect() as conn:
            conn.execute(
                "UPDATE historical_documents SET effective_start = ?, metadata_json = ? WHERE id = ?",
                (current_effective_start, metadata_json, int(doc.id)),
            )
            conn.commit()
    except Exception:
        return


def _normalize_effective_date(value: str | None) -> str | None:
    if not value:
        return None
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %Y", "%b %Y"):
        try:
            parsed = datetime.strptime(value.strip(), fmt)
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


__all__ = [
    "remediate_and_promote_missing_doc_targets",
    "remediate_confidence_below_threshold_discovery_records",
    "remediate_missing_effective_start_historical_documents",
    "remediate_no_downloadable_url_discovery_records",
]
