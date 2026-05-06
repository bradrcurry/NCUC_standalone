from __future__ import annotations

from urllib.parse import urlparse

from duke_rates.models.docket_lead import RegulatoryDocketLeadRecord
from duke_rates.models.historical_lead import HistoricalLeadRecord
from duke_rates.models.url_variant import CandidateUrlVariantRecord


def score_historical_lead(lead: HistoricalLeadRecord) -> tuple[float, list[str]]:
    score = 0.0
    notes: list[str] = []
    extracted_url = lead.extracted_url or ""
    host = urlparse(extracted_url).netloc.lower()

    if host.endswith("duke-energy.com") or host.endswith("progress-energy.com"):
        score += 45
        notes.append("direct Duke/Progress host")
    elif "web.archive.org" in host:
        score += 35
        notes.append("archived Duke/Progress host")
    elif "ncuc.gov" in host or "starw1.ncuc.gov" in host:
        score += 40
        notes.append("regulator host")
    elif extracted_url:
        score += 20
        notes.append("external cited URL")

    if lead.filename:
        score += 12
        notes.append("exact filename extracted")
    if lead.leaf_reference and lead.target_leaf_no and lead.target_leaf_no in lead.leaf_reference:
        score += 20
        notes.append("matching leaf reference")
    if (
        lead.schedule_code
        and lead.target_code
        and lead.schedule_code.upper() == lead.target_code.upper()
    ):
        score += 18
        notes.append("matching schedule code")
    if lead.rider_code and lead.target_code and lead.rider_code.upper() == lead.target_code.upper():
        score += 18
        notes.append("matching rider code")
    if lead.effective_start:
        score += 10
        notes.append("effective date anchor")
    if lead.docket_number:
        score += 8
        notes.append("docket reference")

    provenance_bonus = {
        "utility": 15,
        "regulator": 14,
        "archive": 10,
        "reference": 5,
        "external": 4,
    }.get(lead.provenance_class, 0)
    if provenance_bonus:
        score += provenance_bonus
        notes.append(f"provenance={lead.provenance_class}")

    return (round(score, 2), notes)


def score_url_variant(variant: CandidateUrlVariantRecord) -> tuple[float, list[str]]:
    score = 0.0
    notes: list[str] = []
    host = variant.hostname.lower()
    if host.endswith("duke-energy.com") or host.endswith("progress-energy.com"):
        score += 40
        notes.append("candidate Duke/Progress host")
    if variant.filename and variant.filename.endswith(".pdf"):
        score += 10
        notes.append("pdf filename")
    if variant.direct_downloadable:
        score += 40
        notes.append("directly downloadable")
    elif variant.direct_status_code in {301, 302, 403, 404}:
        score += 5
        notes.append(f"direct status {variant.direct_status_code}")
    if variant.wayback_snapshot_count:
        score += min(25, 5 + variant.wayback_snapshot_count * 2)
        notes.append(f"{variant.wayback_snapshot_count} wayback snapshots")
    if variant.heuristic:
        score += 5
        notes.append(f"heuristic={variant.heuristic}")
    return (round(score, 2), notes)


def score_docket_lead(lead: RegulatoryDocketLeadRecord) -> tuple[float, list[str]]:
    score = 0.0
    notes: list[str] = []
    if lead.docket_number:
        score += 25
        notes.append("explicit docket number")
    if lead.contains_tariff_text:
        score += 30
        notes.append("contains tariff text")
    if not lead.clue_only:
        score += 20
        notes.append("direct filing evidence")
    if lead.proceeding_type:
        score += 10
        notes.append(f"proceeding={lead.proceeding_type}")
    if lead.referenced_codes:
        score += 12
        notes.append("references target codes")
    return (round(score, 2), notes)
