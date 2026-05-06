from __future__ import annotations

import json

from duke_rates.db.repository import Repository
from duke_rates.historical.bill_relevant_gaps import ProgressNCBillRelevantGapService
from duke_rates.historical.family_targets import build_progress_nc_family_targets
from duke_rates.historical.notice_links import ProgressNCNoticeLinkService
from duke_rates.models.evidence_anchor import EvidenceAnchorRecord
from duke_rates.models.search_pack import HistoricalSearchPackRecord


class ProgressNCSearchPackService:
    def __init__(
        self,
        repository: Repository,
        *,
        state: str = "NC",
        company: str = "progress",
    ):
        self.repository = repository
        self.state = state
        self.company = company
        self.notice_links = ProgressNCNoticeLinkService(repository, state=state, company=company)

    def generate_missing_family_packs(self) -> list[HistoricalSearchPackRecord]:
        gap_records = ProgressNCBillRelevantGapService(
            self.repository, state=self.state, company=self.company
        ).build_records()
        targets = build_progress_nc_family_targets(
            self.repository, missing_only=False, state=self.state, company=self.company
        )
        notice_links = self.notice_links.build_links()
        packs: list[HistoricalSearchPackRecord] = []
        for gap in gap_records:
            if "missing_historical_leaf" not in gap.gap_flags:
                continue
            target = targets.get(gap.leaf_no)
            if not target:
                continue
            leads = self.repository.list_historical_leads(family_key=target.family_key)
            variants = self.repository.list_candidate_url_variants(family_key=target.family_key)
            dockets = self.repository.list_regulatory_docket_leads(family_key=target.family_key)
            anchors = self.repository.list_evidence_anchors(family_key=target.family_key)
            notice_refs = [
                {
                    "title": notice.title,
                    "docket_numbers": notice.docket_numbers,
                    "basis": [
                        match.basis
                        for match in notice.matches
                        if match.family_key == target.family_key
                    ],
                }
                for notice in notice_links
                if any(match.family_key == target.family_key for match in notice.matches)
            ]
            observed_history = _bill_observed_first_appearance(
                repository=self.repository,
                component_keys=gap.observed_component_keys,
            )
            for observation in observed_history:
                self.repository.upsert_evidence_anchor(
                    EvidenceAnchorRecord(
                        family_key=target.family_key,
                        anchor_type="bill_observed_component",
                        anchor_value=observation["component_key"],
                        start_date=observation["first_seen"],
                        source_type="bill_observation",
                        source_location="bill_component_observations",
                        confidence_score=45.0,
                        notes=["Derived from parsed Duke bills."],
                        metadata_json=json.dumps(observation, sort_keys=True),
                    )
                )
            payload = {
                "family_key": target.family_key,
                "family_type": target.family_type,
                "category": target.category,
                "leaf_no": target.leaf_no,
                "code": target.code,
                "title": target.title,
                "aliases": list(target.aliases),
                "known_effective_anchor": target.effective_start,
                "observed_components": gap.observed_component_keys,
                "bill_observed_first_appearance": observed_history,
                "related_notice_notes": gap.notes,
                "related_notice_refs": notice_refs,
                "current_applicable_schedules": gap.current_applicable_schedules,
                "lead_count": len(leads),
                "candidate_dockets": sorted({item.docket_number for item in dockets}),
                "candidate_host_families": sorted(
                    {(lead.hostname or "").lower() for lead in leads if lead.hostname}
                )
                or [
                    "www.duke-energy.com",
                    "duke-energy.com",
                    "www.progress-energy.com",
                    "progress-energy.com",
                ],
                "candidate_path_families": sorted(
                    {
                        lead.path_fragment.rsplit("/", 1)[0] + "/"
                        for lead in leads
                        if lead.path_fragment and "/" in lead.path_fragment
                    }
                )
                or [target.current_path.rsplit("/", 1)[0] + "/"],
                "candidate_filename_patterns": sorted(
                    {
                        lead.filename
                        for lead in leads
                        if lead.filename
                    }
                    | {target.current_filename}
                ),
                "query_patterns": _query_patterns(target, dockets),
                "wayback_queries": _wayback_patterns(target, leads, variants),
                "provenance_notes": [
                    "Leads are discovery artifacts, not source-of-truth tariff records.",
                    (
                        "Recovered coverage should only count if predecessor "
                        "status is proven by date or lineage."
                    ),
                ],
                "top_leads": [
                    {
                        "score": lead.confidence_score,
                        "source_class": lead.source_class,
                        "provenance_class": lead.provenance_class,
                        "extracted_url": lead.extracted_url,
                        "docket_number": lead.docket_number,
                        "notes": lead.score_notes,
                    }
                    for lead in leads[:10]
                ],
                "top_variants": [
                    {
                        "score": variant.score,
                        "variant_url": variant.variant_url,
                        "direct_status_code": variant.direct_status_code,
                        "direct_downloadable": variant.direct_downloadable,
                        "wayback_snapshot_count": variant.wayback_snapshot_count,
                        "heuristic": variant.heuristic,
                    }
                    for variant in variants[:10]
                ],
                "anchors": [
                    {
                        "anchor_type": anchor.anchor_type,
                        "anchor_value": anchor.anchor_value,
                        "start_date": anchor.start_date,
                        "end_date": anchor.end_date,
                        "source_type": anchor.source_type,
                    }
                    for anchor in anchors
                ],
            }
            record = HistoricalSearchPackRecord(
                family_key=target.family_key,
                target_leaf_no=target.leaf_no,
                target_code=target.code,
                target_title=target.title,
                family_type=target.family_type,
                payload_json=json.dumps(payload, sort_keys=True),
                notes=[
                    f"gap_flags={','.join(gap.gap_flags)}",
                    f"historical_versions={gap.historical_version_count}",
                ],
            )
            self.repository.upsert_search_pack(record)
            stored = self.repository.get_search_pack(target.family_key)
            if stored:
                packs.append(stored)
        return packs


def _query_patterns(target, dockets) -> list[str]:
    patterns = [
        f"site:duke-energy.com \"{target.title}\"",
        f"site:duke-energy.com \"{target.code}\"" if target.code else "",
        f"site:duke-energy.com \"Leaf No. {target.leaf_no}\"" if target.leaf_no else "",
        f"site:ncuc.gov \"{target.code}\" \"Duke Energy Progress\"" if target.code else "",
    ]
    for docket in dockets[:3]:
        patterns.append(
            f"site:ncuc.gov \"{docket.docket_number}\" "
            f"\"{target.code or target.title}\""
        )
    return [item for item in patterns if item]


def _wayback_patterns(target, leads, variants) -> list[str]:
    patterns = [target.current_url]
    patterns.extend(
        lead.extracted_url
        for lead in leads
        if lead.extracted_url and lead.extracted_url not in patterns
    )
    patterns.extend(
        variant.variant_url
        for variant in variants
        if variant.variant_url not in patterns
    )
    return patterns[:12]


def _bill_observed_first_appearance(
    *,
    repository: Repository,
    component_keys: list[str],
) -> list[dict[str, str | float | None]]:
    observations = repository.list_bill_component_observations()
    rows: list[dict[str, str | float | None]] = []
    for component_key in component_keys:
        matching = [
            item
            for item in observations
            if item.component_key == component_key and item.rate_code == "RES"
        ]
        if not matching:
            continue
        matching.sort(
            key=lambda item: (
                item.period_start or item.service_start or item.service_end,
                item.bill_id,
            )
        )
        first = matching[0]
        rows.append(
            {
                "component_key": component_key,
                "first_seen": (
                    (first.period_start or first.service_start or first.service_end).isoformat()
                    if (first.period_start or first.service_start or first.service_end)
                    else None
                ),
                "first_amount": first.amount,
            }
        )
    return rows
