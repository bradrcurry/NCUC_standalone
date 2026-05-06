from __future__ import annotations

from duke_rates.db.repository import Repository
from duke_rates.historical.lead_scoring import (
    score_docket_lead,
    score_historical_lead,
    score_url_variant,
)


class ProgressNCLeadRegistryService:
    def __init__(self, repository: Repository):
        self.repository = repository

    def rescore_all(self) -> dict[str, int]:
        lead_count = 0
        for lead in self.repository.list_historical_leads():
            score, notes = score_historical_lead(lead)
            lead.confidence_score = score
            lead.score_notes = notes
            self.repository.upsert_historical_lead(lead)
            lead_count += 1

        variant_count = 0
        for variant in self.repository.list_candidate_url_variants():
            score, notes = score_url_variant(variant)
            variant.score = score
            variant.notes = list(dict.fromkeys([*variant.notes, *notes]))
            self.repository.upsert_candidate_url_variant(variant)
            variant_count += 1

        docket_count = 0
        for docket in self.repository.list_regulatory_docket_leads():
            score, notes = score_docket_lead(docket)
            docket.confidence_score = score
            docket.notes = list(dict.fromkeys([*docket.notes, *notes]))
            self.repository.upsert_regulatory_docket_lead(docket)
            docket_count += 1
        return {
            "historical_leads": lead_count,
            "candidate_url_variants": variant_count,
            "regulatory_docket_leads": docket_count,
        }
