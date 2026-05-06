from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from duke_rates.analytics.nc_missing_clean_doc_audit import build_nc_missing_clean_doc_audit
from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.historical.ncuc import search_persistence
from duke_rates.historical.ncuc.discovery import NcucDiscoveryService
from duke_rates.historical.ncuc.document_param_search import DocParamSearchResult, DocumentParamSearcher
from duke_rates.historical.ncuc.query_builder import QuerySpec
from duke_rates.historical.ncuc.result_harvester import HarvestSession, SearchResult
from duke_rates.historical.ncuc.result_scorer import ResultScorer
from duke_rates.historical.ncuc.session import close_authenticated_context, create_authenticated_context
from duke_rates.models.docket_lead import RegulatoryDocketLeadRecord
from duke_rates.models.historical_lead import HistoricalLeadRecord
from duke_rates.models.ncuc import (
    NcucAcquisitionMethod,
    NcucDiscoveryRecord,
    NcucFilingClassification,
)

_MIN_PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2}
_UTILITY_COMPANY = {
    "DEP": "Duke Energy Progress",
    "DEC": "Duke Energy Carolinas",
}


def search_nc_missing_clean_documents(
    settings: Settings,
    repository: Repository,
    *,
    database_path: Path | None = None,
    limit: int = 20,
    min_priority: str = "medium",
    family_key: str | None = None,
    structured_max_results: int = 50,
    keyword_max_results: int = 20,
    max_candidates_per_family: int = 12,
    enrich_portal_details: bool = True,
    persist: bool = True,
    save_manifest: bool = True,
) -> dict[str, Any]:
    report = build_nc_missing_clean_doc_audit(database_path)
    selected_rows = _select_audit_rows(
        report.get("rows", []),
        limit=limit,
        min_priority=min_priority,
        family_key=family_key,
    )

    if not selected_rows:
        return {
            "generated_at": report.get("generated_at"),
            "lead_count": 0,
            "rows": [],
            "persisted_discovery_count": 0,
            "persisted_historical_lead_count": 0,
            "persisted_docket_lead_count": 0,
            "harvest_path": None,
        }

    scorer = ResultScorer()
    harvest_session = HarvestSession()
    rows: list[dict[str, Any]] = []
    persisted_discovery_count = 0
    persisted_historical_lead_count = 0
    persisted_docket_lead_count = 0

    portal_context = None
    if _should_use_structured_portal(settings):
        portal_context = create_authenticated_context(settings)
    discovery_service = NcucDiscoveryService(settings)
    try:
        for lead_row in selected_rows:
            structured_candidates = _run_structured_searches(
                settings,
                portal_context=portal_context,
                harvest_session=harvest_session,
                lead_row=lead_row,
                max_results=structured_max_results,
                enrich_portal_details=enrich_portal_details,
            )
            keyword_candidates = _run_keyword_searches(
                settings,
                discovery_service=discovery_service,
                harvest_session=harvest_session,
                lead_row=lead_row,
                max_results=keyword_max_results,
            )

            merged = _merge_candidate_rows(structured_candidates + keyword_candidates)
            scored = scorer.score_all([item["search_result"] for item in merged])
            scored_by_url = {item.result.url: item for item in scored}

            family_candidates: list[dict[str, Any]] = []
            for item in merged:
                scored_item = scored_by_url.get(item["search_result"].url)
                if scored_item is None:
                    continue
                item["score"] = scored_item.combined_score
                item["doc_type_guess"] = scored_item.ideality.doc_type_guess
                item["likely_finality"] = scored_item.ideality.likely_finality
                item["is_ideal_candidate"] = scored_item.ideality.is_ideal_candidate
                item["score_explanation"] = scored_item.explain()
                family_candidates.append(item)

            family_candidates.sort(
                key=lambda item: (
                    float(item.get("score") or 0.0),
                    1 if item.get("download_url") else 0,
                    1 if item.get("viewer_url") else 0,
                    item["search_result"].filing_date or "",
                ),
                reverse=True,
            )
            family_candidates = family_candidates[:max_candidates_per_family]

            persisted_ids = {"discovery": [], "historical": [], "docket": []}
            if persist:
                for candidate in family_candidates:
                    discovery_id, historical_lead_id, docket_lead_id = _persist_candidate(
                        repository,
                        lead_row=lead_row,
                        candidate=candidate,
                    )
                    if discovery_id:
                        persisted_discovery_count += 1
                        persisted_ids["discovery"].append(discovery_id)
                    if historical_lead_id:
                        persisted_historical_lead_count += 1
                        persisted_ids["historical"].append(historical_lead_id)
                    if docket_lead_id:
                        persisted_docket_lead_count += 1
                        persisted_ids["docket"].append(docket_lead_id)

            rows.append(
                {
                    "family_key": lead_row["family_key"],
                    "priority_band": lead_row["priority_band"],
                    "priority_score": lead_row["priority_score"],
                    "missing_kind": lead_row["missing_kind"],
                    "candidate_count": len(family_candidates),
                    "structured_candidate_count": len(structured_candidates),
                    "keyword_candidate_count": len(keyword_candidates),
                    "persisted_discovery_ids": persisted_ids["discovery"],
                    "persisted_historical_lead_ids": persisted_ids["historical"],
                    "persisted_docket_lead_ids": persisted_ids["docket"],
                    "top_candidates": [
                        _candidate_summary(item)
                        for item in family_candidates[:5]
                    ],
                }
            )
    finally:
        discovery_service.close()
        if portal_context is not None:
            pw, ctx, _ = portal_context
            close_authenticated_context(pw, ctx)

    harvest_path = None
    if save_manifest:
        harvest_path = search_persistence.save_harvest_session(harvest_session, settings)

    return {
        "generated_at": report.get("generated_at"),
        "lead_count": len(selected_rows),
        "rows": rows,
        "persisted_discovery_count": persisted_discovery_count,
        "persisted_historical_lead_count": persisted_historical_lead_count,
        "persisted_docket_lead_count": persisted_docket_lead_count,
        "harvest_path": str(harvest_path) if harvest_path else None,
    }


def _select_audit_rows(
    rows: list[dict[str, Any]],
    *,
    limit: int,
    min_priority: str,
    family_key: str | None,
) -> list[dict[str, Any]]:
    min_rank = _MIN_PRIORITY_RANK.get((min_priority or "medium").lower(), 1)
    selected = []
    for row in rows:
        row_priority = str(row.get("priority_band") or "low").lower()
        if _MIN_PRIORITY_RANK.get(row_priority, 0) < min_rank:
            continue
        if family_key and str(row.get("family_key")) != family_key:
            continue
        selected.append(row)
        if limit and len(selected) >= limit:
            break
    return selected


def _should_use_structured_portal(settings: Settings) -> bool:
    return bool(settings.ncid_username and settings.ncid_password)


def _run_structured_searches(
    settings: Settings,
    *,
    portal_context: tuple[Any, Any, Any] | None,
    harvest_session: HarvestSession,
    lead_row: dict[str, Any],
    max_results: int,
    enrich_portal_details: bool,
) -> list[dict[str, Any]]:
    if portal_context is None:
        return []

    _, _, page = portal_context
    searcher = DocumentParamSearcher(settings)
    results: list[dict[str, Any]] = []
    for spec in _build_structured_query_specs(lead_row):
        raw_rows = searcher.search(
            page,
            company_name=_company_name_for_row(lead_row),
            docket_number=_query_note_value(spec.notes, "docket") or "",
            filing_types=_note_csv_list(spec.notes, "filing_types") or _loads_json_list(lead_row.get("suggested_portal_filing_types")),
            date_after=_query_note_value(spec.notes, "date_after") or "",
            date_before=_query_note_value(spec.notes, "date_before") or "",
            max_results=max_results,
        )
        if enrich_portal_details and raw_rows:
            raw_rows = searcher.enrich_with_document_details(page, raw_rows, delay_seconds=0.2)
        converted = [_doc_param_candidate(lead_row, spec, row) for row in raw_rows]
        harvest_session.record_query(spec, [item["search_result"] for item in converted], False, "")
        results.extend(converted)
    return results


def _run_keyword_searches(
    settings: Settings,
    *,
    discovery_service: NcucDiscoveryService,
    harvest_session: HarvestSession,
    lead_row: dict[str, Any],
    max_results: int,
) -> list[dict[str, Any]]:
    from duke_rates.models.ncuc import NcucSearchQuery

    results: list[dict[str, Any]] = []
    for query in _build_keyword_queries(lead_row):
        spec = QuerySpec(
            query_text=query.query_text,
            template_name="missing_clean_doc_keyword_search",
            utility_hint=_company_name_for_row(lead_row),
            doc_type_hint=str(lead_row.get("family_type") or "tariff"),
            schedule_code_hint=str(lead_row.get("schedule_code") or "") or None,
            family_key_hint=str(lead_row["family_key"]),
            priority=float(lead_row.get("priority_score") or 0),
            notes=[
                "source=ncuc_public_search",
                f"family_key={lead_row['family_key']}",
                *( [f"docket={query.docket_hint}"] if query.docket_hint else [] ),
            ],
        )
        discovered = [
            _discovery_candidate(lead_row, spec, row.record, row.relevance_score, row.notes)
            for row in discovery_service.search_public_site(query, max_results=max_results)
        ]
        harvest_session.record_query(spec, [item["search_result"] for item in discovered], False, "")
        results.extend(discovered)
    return results


def _build_structured_query_specs(lead_row: dict[str, Any]) -> list[QuerySpec]:
    dockets = _loads_json_list(lead_row.get("suggested_dockets"))
    filing_type_values = _loads_json_list(lead_row.get("suggested_portal_filing_types")) or ["TARIFF", "RATESCED", "ORDER"]
    filing_types = ",".join(filing_type_values)
    base_notes = [
        "source=missing_clean_doc_audit",
        f"missing_kind={lead_row['missing_kind']}",
        f"family_key={lead_row['family_key']}",
        f"priority_band={lead_row['priority_band']}",
        f"filing_types={filing_types}",
    ]
    if lead_row.get("suggested_date_after"):
        base_notes.append(f"date_after={lead_row['suggested_date_after']}")
    if lead_row.get("suggested_date_before"):
        base_notes.append(f"date_before={lead_row['suggested_date_before']}")

    query_terms = _loads_json_list(lead_row.get("suggested_query_terms"))
    query_text = "missing-clean-doc " + " | ".join(query_terms[:3] or [str(lead_row["family_key"])])
    specs: list[QuerySpec] = []
    exact_dockets = _normalize_docket_list(dockets[:5])
    if exact_dockets:
        for docket in exact_dockets:
            specs.append(
                QuerySpec(
                    query_text=query_text,
                    template_name="missing_clean_doc_structured_portal_search",
                    utility_hint=_company_name_for_row(lead_row),
                    doc_type_hint=str(lead_row.get("family_type") or "tariff"),
                    schedule_code_hint=str(lead_row.get("schedule_code") or "") or None,
                    family_key_hint=str(lead_row["family_key"]),
                    priority=float(lead_row.get("priority_score") or 0),
                    notes=[*base_notes, "search_scope=exact_docket", f"docket={docket}"],
                )
            )

        expanded_dockets = _expand_search_dockets(exact_dockets)
        for docket in expanded_dockets:
            specs.append(
                QuerySpec(
                    query_text=query_text,
                    template_name="missing_clean_doc_structured_portal_search",
                    utility_hint=_company_name_for_row(lead_row),
                    doc_type_hint=str(lead_row.get("family_type") or "tariff"),
                    schedule_code_hint=str(lead_row.get("schedule_code") or "") or None,
                    family_key_hint=str(lead_row["family_key"]),
                    priority=max(0.5, float(lead_row.get("priority_score") or 0) - 1.0),
                    notes=[*base_notes, "search_scope=expanded_docket", "neighbor_dockets=false", f"docket={docket}"],
                )
            )

    broad_filing_types = _broaden_portal_filing_types(filing_type_values)
    specs.append(
        QuerySpec(
            query_text=query_text,
            template_name="missing_clean_doc_structured_portal_search",
            utility_hint=_company_name_for_row(lead_row),
            doc_type_hint=str(lead_row.get("family_type") or "tariff"),
            schedule_code_hint=str(lead_row.get("schedule_code") or "") or None,
            family_key_hint=str(lead_row["family_key"]),
            priority=max(0.25, float(lead_row.get("priority_score") or 0) - 2.0),
            notes=[
                *[
                    note
                    for note in base_notes
                    if not note.startswith("filing_types=")
                ],
                "search_scope=docketless_broad",
                f"filing_types={','.join(broad_filing_types)}",
            ],
        )
    )

    return _dedupe_query_specs(specs)


def _build_keyword_queries(lead_row: dict[str, Any]) -> list[Any]:
    from duke_rates.models.ncuc import NcucSearchQuery

    company_name = _company_name_for_row(lead_row)
    docket_hints = _expand_search_dockets(_loads_json_list(lead_row.get("suggested_dockets"))[:3], max_expanded=7)
    schedule_code = str(lead_row.get("schedule_code") or "") or None
    terms = _build_keyword_terms(lead_row)
    redline_hint = str(lead_row.get("redline_search_hint") or "").strip()
    if redline_hint:
        terms.append(redline_hint)

    queries: list[NcucSearchQuery] = []
    seen: set[tuple[str, str | None]] = set()
    for term in terms[:7]:
        q = f"\"{company_name}\" \"{term}\""
        for docket_hint in docket_hints[:4]:
            key = (q, docket_hint)
            if key in seen:
                continue
            seen.add(key)
            queries.append(
                NcucSearchQuery(
                    query_text=q,
                    docket_hint=docket_hint,
                    family_key_hint=str(lead_row["family_key"]),
                    schedule_code_hint=schedule_code,
                    date_from=lead_row.get("suggested_date_after"),
                    date_to=lead_row.get("suggested_date_before"),
                )
            )
        key = (q, None)
        if key not in seen:
            seen.add(key)
            queries.append(
                NcucSearchQuery(
                    query_text=q,
                    docket_hint=None,
                    family_key_hint=str(lead_row["family_key"]),
                    schedule_code_hint=schedule_code,
                    date_from=lead_row.get("suggested_date_after"),
                    date_to=lead_row.get("suggested_date_before"),
                )
            )
    return queries


def _build_keyword_terms(lead_row: dict[str, Any]) -> list[str]:
    family_key = str(lead_row.get("family_key") or "")
    schedule_code = str(lead_row.get("schedule_code") or "").strip()
    title = str(lead_row.get("title") or "").strip()
    family_type = str(lead_row.get("family_type") or "").strip().replace("_", " ")
    terms = _loads_json_list(lead_row.get("suggested_query_terms"))
    if schedule_code:
        terms.extend(
            [
                schedule_code,
                f"Schedule {schedule_code}",
                f"Rate Schedule {schedule_code}",
                f"{schedule_code} tariff",
                f"{schedule_code} compliance tariff",
            ]
        )
    leaf_no = _leaf_from_family_key(family_key)
    if leaf_no:
        terms.extend([f"Leaf {leaf_no}", f"Sheet {leaf_no}"])
    if title:
        terms.extend([title, f"{title} tariff"])
    if family_type:
        terms.append(family_type)
    return _dedupe_strings(terms)


def _doc_param_candidate(
    lead_row: dict[str, Any],
    spec: QuerySpec,
    row: DocParamSearchResult,
) -> dict[str, Any]:
    download_url = row.view_file_urls[0] if row.view_file_urls else None
    search_result = SearchResult(
        url=row.document_detail_url or download_url or "",
        title=row.description or None,
        snippet=" | ".join(
            part for part in [
                row.doc_type,
                row.company_name,
                row.docket_number,
                row.filing_classification,
            ] if part
        ) or None,
        filing_date=row.date_filed or None,
        docket_number=row.docket_number or None,
        sub_number=_sub_number(row.docket_number),
        source_query=spec.query_text,
        source_template=spec.template_name,
        utility_hint=spec.utility_hint,
        doc_type_hint=spec.doc_type_hint,
        schedule_code_hint=spec.schedule_code_hint,
        rider_code_hint=None,
        extracted_schedule_codes=list(row.extracted_schedule_codes),
        extracted_rider_codes=list(row.extracted_rider_codes),
        filing_classification=row.filing_classification or "other",
        found_by_queries=[spec.query_text],
    )
    return {
        "source_type": "structured_portal",
        "search_result": search_result,
        "raw_result": row,
        "download_url": download_url,
        "viewer_url": download_url,
        "document_detail_url": row.document_detail_url,
        "metadata": {
            "doc_param_result": asdict(row),
            "audit_context": _audit_context(lead_row),
            "query_notes": list(spec.notes),
        },
    }


def _discovery_candidate(
    lead_row: dict[str, Any],
    spec: QuerySpec,
    record: NcucDiscoveryRecord,
    relevance_score: float,
    notes: list[str],
) -> dict[str, Any]:
    search_result = SearchResult(
        url=record.discovered_url or record.download_url or record.viewer_url or "",
        title=record.filing_title or None,
        snippet=" | ".join(
            part for part in [
                record.proceeding_type,
                record.docket_number,
                record.filing_classification.value,
            ] if part
        ) or None,
        filing_date=record.filing_date,
        docket_number=record.docket_number,
        sub_number=record.sub_number,
        source_query=spec.query_text,
        source_template=spec.template_name,
        utility_hint=spec.utility_hint,
        doc_type_hint=spec.doc_type_hint,
        schedule_code_hint=spec.schedule_code_hint,
        rider_code_hint=record.referenced_rider_codes[0] if record.referenced_rider_codes else None,
        extracted_schedule_codes=list(record.referenced_schedule_codes),
        extracted_rider_codes=list(record.referenced_rider_codes),
        filing_classification=record.filing_classification.value,
        found_by_queries=[spec.query_text],
    )
    return {
        "source_type": "keyword_public_search",
        "search_result": search_result,
        "raw_result": record,
        "download_url": record.download_url or record.viewer_url or record.attachment_url,
        "viewer_url": record.viewer_url,
        "document_detail_url": record.discovered_url,
        "metadata": {
            "discovery_record": record.model_dump(mode="json"),
            "audit_context": _audit_context(lead_row),
            "query_notes": list(spec.notes),
            "keyword_relevance_score": relevance_score,
            "keyword_notes": list(notes),
        },
    }


def _merge_candidate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in rows:
        key = item["search_result"].url or item.get("download_url") or item.get("viewer_url")
        if not key:
            continue
        if key not in merged:
            merged[key] = item
            continue
        existing = merged[key]
        existing["search_result"].found_by_queries.extend(
            query
            for query in item["search_result"].found_by_queries
            if query not in existing["search_result"].found_by_queries
        )
        if not existing.get("download_url") and item.get("download_url"):
            existing["download_url"] = item["download_url"]
        if not existing.get("viewer_url") and item.get("viewer_url"):
            existing["viewer_url"] = item["viewer_url"]
        if not existing.get("document_detail_url") and item.get("document_detail_url"):
            existing["document_detail_url"] = item["document_detail_url"]
    return list(merged.values())


def _persist_candidate(
    repository: Repository,
    *,
    lead_row: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[int | None, int | None, int | None]:
    discovery_record = _build_discovery_record(lead_row, candidate)
    discovery_id = repository.upsert_ncuc_discovery_record(discovery_record)
    discovery_record = discovery_record.model_copy(update={"id": discovery_id})

    historical_lead = _build_historical_lead(lead_row, candidate, discovery_id)
    historical_lead_id = repository.upsert_historical_lead(historical_lead)

    docket_lead_id = None
    if discovery_record.docket_number:
        docket_lead = _build_docket_lead(lead_row, candidate, discovery_record)
        docket_lead_id = repository.upsert_regulatory_docket_lead(docket_lead)

    return discovery_id, historical_lead_id, docket_lead_id


def _build_discovery_record(
    lead_row: dict[str, Any],
    candidate: dict[str, Any],
) -> NcucDiscoveryRecord:
    raw = candidate["raw_result"]
    if isinstance(raw, NcucDiscoveryRecord):
        metadata = dict(candidate["metadata"])
        metadata["missing_clean_doc_search"] = True
        record = raw.model_copy(
            update={
                "family_keys": sorted(set([*raw.family_keys, str(lead_row["family_key"])])),
                "provenance_notes": [
                    *raw.provenance_notes,
                    f"missing_kind={lead_row['missing_kind']}",
                    f"priority_band={lead_row['priority_band']}",
                ],
                "search_confidence_score": float(candidate.get("score") or 0.0),
                "search_ideality": _candidate_search_ideality(candidate),
                "metadata_json": json.dumps(metadata),
            }
        )
        return record

    assert isinstance(raw, DocParamSearchResult)
    viewer_url = candidate.get("viewer_url")
    detail_url = candidate.get("document_detail_url")
    metadata = dict(candidate["metadata"])
    metadata["missing_clean_doc_search"] = True
    return NcucDiscoveryRecord(
        docket_number=raw.docket_number or None,
        sub_number=_sub_number(raw.docket_number),
        utility=_company_name_for_row(lead_row),
        filing_title=raw.description or None,
        filing_date=raw.date_filed or None,
        proceeding_type=raw.filing_classification or None,
        filing_classification=_classification_from_text(raw.filing_classification),
        referenced_schedule_codes=list(raw.extracted_schedule_codes),
        referenced_rider_codes=list(raw.extracted_rider_codes),
        family_keys=[str(lead_row["family_key"])],
        discovered_url=detail_url or viewer_url,
        viewer_url=viewer_url,
        attachment_url=viewer_url if viewer_url and viewer_url.lower().endswith(".pdf") else None,
        download_url=viewer_url,
        acquisition_method=NcucAcquisitionMethod.PLAYWRIGHT,
        doc_quality_tier="T2",
        search_confidence_score=float(candidate.get("score") or 0.0),
        search_ideality=_candidate_search_ideality(candidate),
        provenance_notes=[
            "source=missing_clean_doc_structured_portal_search",
            f"missing_kind={lead_row['missing_kind']}",
            f"priority_band={lead_row['priority_band']}",
            f"priority_score={lead_row['priority_score']}",
        ],
        metadata_json=json.dumps(metadata),
    )


def _build_historical_lead(
    lead_row: dict[str, Any],
    candidate: dict[str, Any],
    discovery_id: int | None,
) -> HistoricalLeadRecord:
    search_result = candidate["search_result"]
    download_url = candidate.get("download_url")
    detail_url = candidate.get("document_detail_url")
    path = urlparse(download_url or detail_url or "")
    metadata = dict(candidate["metadata"])
    metadata["discovery_record_id"] = discovery_id
    metadata["missing_clean_doc_search"] = True
    return HistoricalLeadRecord(
        family_key=str(lead_row["family_key"]),
        target_leaf_no=str(lead_row.get("leaf_no") or "") or None,
        target_code=str(lead_row.get("schedule_code") or "") or None,
        target_title=str(lead_row.get("title") or lead_row["family_key"]),
        family_type=str(lead_row.get("family_type") or "rate_schedule"),
        category="tariff",
        source_class="ncuc_missing_doc_search",
        provenance_class="regulator",
        source_label=candidate["source_type"],
        source_location=detail_url or download_url or search_result.url,
        source_url=detail_url or download_url or search_result.url,
        extracted_url=download_url or detail_url or search_result.url,
        extracted_title=search_result.title,
        attachment_url=download_url if download_url and download_url.lower().endswith(".pdf") else None,
        viewer_url=download_url,
        hostname=path.netloc or None,
        path_fragment=path.path or None,
        filename=Path(path.path).name or None,
        docket_number=search_result.docket_number,
        schedule_code=(search_result.extracted_schedule_codes[0] if search_result.extracted_schedule_codes else str(lead_row.get("schedule_code") or "") or None),
        rider_code=(search_result.extracted_rider_codes[0] if search_result.extracted_rider_codes else None),
        extraction_method=f"{candidate['source_type']}_missing_clean_doc_search",
        confidence_score=float(candidate.get("score") or 0.0),
        disposition="new",
        score_notes=[
            f"doc_type_guess={candidate.get('doc_type_guess')}",
            f"likely_finality={candidate.get('likely_finality')}",
            f"is_ideal_candidate={candidate.get('is_ideal_candidate')}",
        ],
        notes=[
            f"missing_kind={lead_row['missing_kind']}",
            f"priority_band={lead_row['priority_band']}",
            f"evidence={lead_row['evidence_summary']}",
        ],
        metadata_json=json.dumps(metadata),
    )


def _build_docket_lead(
    lead_row: dict[str, Any],
    candidate: dict[str, Any],
    discovery_record: NcucDiscoveryRecord,
) -> RegulatoryDocketLeadRecord:
    metadata = dict(candidate["metadata"])
    metadata["missing_clean_doc_search"] = True
    referenced_codes = []
    if lead_row.get("schedule_code"):
        referenced_codes.append(str(lead_row["schedule_code"]))
    referenced_codes.extend(discovery_record.referenced_schedule_codes)
    referenced_codes.extend(discovery_record.referenced_rider_codes)
    referenced_codes = sorted({code for code in referenced_codes if code})
    return RegulatoryDocketLeadRecord(
        family_key=str(lead_row["family_key"]),
        docket_number=str(discovery_record.docket_number or "unknown"),
        utility=discovery_record.utility,
        proceeding_type=discovery_record.proceeding_type,
        date_start=discovery_record.filing_date,
        referenced_codes=referenced_codes,
        evidence_source=discovery_record.filing_title or candidate["source_type"],
        evidence_source_type="ncuc_missing_doc_search",
        evidence_source_location=discovery_record.discovered_url or discovery_record.download_url,
        title=discovery_record.filing_title,
        contains_tariff_text=discovery_record.filing_classification in {
            NcucFilingClassification.TARIFF_SHEETS,
            NcucFilingClassification.COMPLIANCE_FILING,
            NcucFilingClassification.ATTACHMENT,
        },
        clue_only=False,
        confidence_score=float(candidate.get("score") or 0.0),
        notes=[
            f"missing_kind={lead_row['missing_kind']}",
            f"priority_band={lead_row['priority_band']}",
        ],
        metadata_json=json.dumps(metadata),
    )


def _candidate_search_ideality(candidate: dict[str, Any]) -> str:
    if candidate.get("is_ideal_candidate"):
        return "ideal"
    likely_finality = str(candidate.get("likely_finality") or "").lower()
    doc_type_guess = str(candidate.get("doc_type_guess") or "").lower()
    if likely_finality == "final" and doc_type_guess in {"tariff_sheet", "rate_schedule", "rider", "order", "exhibit"}:
        return "probable"
    if likely_finality in {"unknown", "intermediate", "procedural"}:
        return "possible"
    return "possible"


def _candidate_summary(item: dict[str, Any]) -> dict[str, Any]:
    search_result = item["search_result"]
    return {
        "source_type": item["source_type"],
        "title": search_result.title,
        "url": search_result.url,
        "download_url": item.get("download_url"),
        "docket_number": search_result.docket_number,
        "filing_date": search_result.filing_date,
        "score": round(float(item.get("score") or 0.0), 2),
        "search_ideality": _candidate_search_ideality(item),
        "doc_type_guess": item.get("doc_type_guess"),
        "likely_finality": item.get("likely_finality"),
        "is_ideal_candidate": bool(item.get("is_ideal_candidate")),
        "found_by_queries": list(search_result.found_by_queries),
    }


def _audit_context(lead_row: dict[str, Any]) -> dict[str, Any]:
    return {
        "family_key": lead_row["family_key"],
        "missing_kind": lead_row["missing_kind"],
        "priority_band": lead_row["priority_band"],
        "priority_score": lead_row["priority_score"],
        "evidence_summary": lead_row["evidence_summary"],
        "suggested_dockets": _loads_json_list(lead_row.get("suggested_dockets")),
        "suggested_query_terms": _loads_json_list(lead_row.get("suggested_query_terms")),
        "suggested_date_after": lead_row.get("suggested_date_after"),
        "suggested_date_before": lead_row.get("suggested_date_before"),
    }


def _loads_json_list(payload: Any) -> list[str]:
    if not payload:
        return []
    if isinstance(payload, list):
        return [str(item) for item in payload if item]
    try:
        parsed = json.loads(str(payload))
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return [str(item) for item in parsed if item]
    return []


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(cleaned)
    return output


def _normalize_docket_number(docket: str) -> str:
    match = re.search(r"\b([A-Z]-\d+)\s*(?:,)?\s*Sub\s*(\d+)\b", docket or "", re.I)
    if not match:
        return str(docket or "").strip()
    return f"{match.group(1).upper()} Sub {int(match.group(2))}"


def _normalize_docket_list(dockets: list[str]) -> list[str]:
    return _dedupe_strings([_normalize_docket_number(item) for item in dockets if item])


def _expand_search_dockets(dockets: list[str], *, max_expanded: int = 9) -> list[str]:
    expanded: list[str] = []
    for docket in _normalize_docket_list(dockets):
        expanded.append(docket)
        match = re.match(r"^([A-Z]-\d+)\s+Sub\s+(\d+)$", docket, re.I)
        if not match:
            continue
        prefix = match.group(1).upper()
        sub_number = int(match.group(2))
        for delta in (1, 2, 3):
            expanded.append(f"{prefix} Sub {max(1, sub_number - delta)}")
            expanded.append(f"{prefix} Sub {sub_number + delta}")
    return _dedupe_strings(expanded)[:max_expanded]


def _broaden_portal_filing_types(filing_types: list[str]) -> list[str]:
    expanded = [str(item).upper() for item in filing_types if item]
    expanded.extend(["TARIFF", "RATESCED", "ORDER", "INFOFILE"])
    return _dedupe_strings(expanded)


def _dedupe_query_specs(specs: list[QuerySpec]) -> list[QuerySpec]:
    seen: set[tuple[str, tuple[str, ...]]] = set()
    output: list[QuerySpec] = []
    for spec in specs:
        key = (
            spec.query_text,
            tuple(str(note) for note in spec.notes),
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(spec)
    return output


def _query_note_value(notes: list[str], key: str) -> str | None:
    prefix = f"{key}="
    for note in notes:
        if note.startswith(prefix):
            return note[len(prefix):]
    return None


def _note_csv_list(notes: list[str], key: str) -> list[str]:
    value = _query_note_value(notes, key)
    if not value:
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _company_name_for_row(lead_row: dict[str, Any]) -> str:
    return _UTILITY_COMPANY.get(str(lead_row.get("utility") or "").upper(), "Duke Energy Progress")


def _sub_number(docket_number: str | None) -> str | None:
    if not docket_number:
        return None
    parts = str(docket_number).replace(",", " ").split()
    for idx, part in enumerate(parts):
        if part.lower() == "sub" and idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def _leaf_from_family_key(family_key: str | None) -> str | None:
    match = re.search(r"leaf-(\d+)\b", family_key or "", re.I)
    return match.group(1) if match else None


def _classification_from_text(value: str | None) -> NcucFilingClassification:
    raw = (value or "").strip().lower()
    for candidate in NcucFilingClassification:
        if raw == candidate.value:
            return candidate
    if "order" in raw:
        return NcucFilingClassification.ORDER
    if "compliance" in raw:
        return NcucFilingClassification.COMPLIANCE_FILING
    if "tariff" in raw or "ratesced" in raw:
        return NcucFilingClassification.TARIFF_SHEETS
    if "attachment" in raw:
        return NcucFilingClassification.ATTACHMENT
    if "application" in raw:
        return NcucFilingClassification.APPLICATION
    return NcucFilingClassification.OTHER


__all__ = ["search_nc_missing_clean_documents"]
