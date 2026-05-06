from __future__ import annotations

import json

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.historical.citation_miner import HistoricalCitationMiner
from duke_rates.historical.family_targets import build_progress_nc_family_targets
from duke_rates.historical.lead_scoring import score_docket_lead
from duke_rates.historical.notice_links import ProgressNCNoticeLinkService
from duke_rates.models.docket_lead import RegulatoryDocketLeadRecord


class ProgressNCRegulatorLeadService:
    def __init__(self, settings: Settings, repository: Repository):
        self.settings = settings
        self.repository = repository
        self.miner = HistoricalCitationMiner(settings, repository)

    def mine_existing_regulator_leads(self) -> list[RegulatoryDocketLeadRecord]:
        self.miner.mine_imported_documents_progress_nc()
        targets = build_progress_nc_family_targets(self.repository, missing_only=True)
        notice_links = ProgressNCNoticeLinkService(self.repository).build_links()
        for notice in notice_links:
            for match in notice.matches:
                if match.family_key not in {target.family_key for target in targets.values()}:
                    continue
                lead = RegulatoryDocketLeadRecord(
                    family_key=match.family_key,
                    docket_number=notice.docket_numbers[0] if notice.docket_numbers else "unknown",
                    utility="Duke Energy Progress",
                    proceeding_type="notice",
                    referenced_codes=notice.related_rider_codes + notice.related_schedule_codes,
                    evidence_source=notice.title,
                    evidence_source_type="duke_notice",
                    evidence_source_location=f"historical:{notice.historical_id}",
                    title=notice.title,
                    contains_tariff_text=False,
                    clue_only=True,
                    notes=[
                        f"basis={basis.basis}"
                        for basis in notice.matches
                        if basis.family_key == match.family_key
                    ],
                    metadata_json=json.dumps(
                        {
                            "historical_id": notice.historical_id,
                            "docket_numbers": notice.docket_numbers,
                            "related_rider_codes": notice.related_rider_codes,
                            "related_schedule_codes": notice.related_schedule_codes,
                        },
                        sort_keys=True,
                    ),
                )
                score, notes = score_docket_lead(lead)
                lead.confidence_score = score
                lead.notes.extend(notes)
                self.repository.upsert_regulatory_docket_lead(lead)
        return self.repository.list_regulatory_docket_leads()

    def ingest_manual_regulator_lead(
        self,
        *,
        family_query: str,
        title: str,
        source_label: str,
        source_type: str,
        source_url: str | None = None,
        docket_number: str | None = None,
        text: str | None = None,
    ) -> list[RegulatoryDocketLeadRecord]:
        leads = self.miner.ingest_manual_lead(
            family_query=family_query,
            source_class=source_type,
            provenance_class="regulator",
            source_label=source_label,
            source_location=source_url,
            source_url=source_url,
            text=text,
            title=title,
            docket_number=docket_number,
        )
        return self.repository.list_regulatory_docket_leads(
            family_key=leads[0].family_key if leads else None
        )
