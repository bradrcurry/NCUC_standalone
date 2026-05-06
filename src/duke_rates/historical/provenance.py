from __future__ import annotations

import json
from urllib.parse import urlparse

from duke_rates.db.repository import Repository
from duke_rates.historical.lineage import ProgressNCLineageService
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.models.source_provenance import ChainSourceCoverage, SourceProvenance


class ProgressNCProvenanceService:
    def __init__(self, repository: Repository, *, state: str = "NC", company: str = "progress"):
        self.repository = repository
        self.state = state
        self.company = company
        self.lineage = ProgressNCLineageService(repository)

    def list_historical_sources(self) -> list[tuple[HistoricalDocumentRecord, SourceProvenance]]:
        rows = self.repository.list_historical_documents(state=self.state, company=self.company)
        pairs = [(row, derive_source_provenance(row)) for row in rows]
        pairs.sort(
            key=lambda item: (
                -item[1].confidence_rank,
                item[0].title.lower(),
                item[0].id or 0,
            )
        )
        return pairs

    def build_chain_coverage(self, *, query: str | None = None) -> list[ChainSourceCoverage]:
        chains = self.lineage.build_chains(query=query, recovered_only=False)
        historical_rows = self.repository.list_historical_documents(state=self.state, company=self.company)
        by_family: dict[str, list[tuple[HistoricalDocumentRecord, SourceProvenance]]] = {}
        for row in historical_rows:
            by_family.setdefault(row.family_key.lower(), []).append(
                (row, derive_source_provenance(row))
            )

        coverage: list[ChainSourceCoverage] = []
        for chain in chains:
            evidence = [
                provenance
                for _, provenance in by_family.get(chain.family_key.lower(), [])
            ]
            if not evidence:
                continue
            authorities = sorted({item.authority for item in evidence})
            source_types = sorted({item.source_type for item in evidence})
            dockets = sorted({item.docket_number for item in evidence if item.docket_number})
            coverage.append(
                ChainSourceCoverage(
                    family_key=chain.family_key,
                    title=chain.title,
                    leaf_no=chain.leaf_no,
                    category=chain.category,
                    version_count=len(chain.versions),
                    authorities=authorities,
                    source_types=source_types,
                    dockets=dockets,
                    evidence=evidence,
                )
            )
        coverage.sort(
            key=lambda item: (
                -max((e.confidence_rank for e in item.evidence), default=0),
                item.title.lower(),
            )
        )
        return coverage


def derive_source_provenance(row: HistoricalDocumentRecord) -> SourceProvenance:
    metadata = _safe_json_load(row.metadata_json)
    source_label = _metadata_value(metadata, "source_label") or _source_label_from_notes(row.notes)
    source_authority = _metadata_value(metadata, "source_authority")
    source_type_hint = _metadata_value(metadata, "source_type")
    source_url = (
        _metadata_value(metadata, "source_url")
        or _metadata_value(metadata, "page_url")
        or _metadata_value(metadata, "current_document_url")
        or row.canonical_url
    )
    docket_number = _metadata_value(metadata, "docket_number")

    authority, source_type, confidence_rank, notes = _classify_source(
        row=row,
        source_label=source_label,
        source_authority=source_authority,
        source_type_hint=source_type_hint,
        source_url=source_url,
    )
    return SourceProvenance(
        authority=authority,
        source_type=source_type,
        source_label=source_label,
        source_url=source_url,
        docket_number=docket_number,
        confidence_rank=confidence_rank,
        notes=notes,
    )


def _classify_source(
    *,
    row: HistoricalDocumentRecord,
    source_label: str | None,
    source_authority: str | None,
    source_type_hint: str | None,
    source_url: str | None,
) -> tuple[str, str, int, list[str]]:
    if source_authority or source_type_hint:
        return (
            (source_authority or "external").lower(),
            (source_type_hint or source_label or "manual").lower(),
            _authority_rank(source_authority or "external"),
            [],
        )

    label = (source_label or "").lower()
    host = urlparse(source_url or "").netloc.lower()
    notes: list[str] = []

    if "openei" in label or "openei.org" in host:
        return ("reference", "openei_reference", 10, notes)
    if "ncuc" in label or "ncuc.gov" in host or "starw1.ncuc.gov" in host:
        return ("regulator", "ncuc", 90, notes)
    if "public-notice" in label or "public_notice" in row.category:
        notes.append(
            "Public-notice evidence may summarize a filing without "
            "containing the full tariff leaf."
        )
        if "duke-energy.com" in host:
            return ("utility", "duke_public_notice", 70, notes)
    if "web.archive.org" in row.archived_url:
        notes.append(
            "Archived snapshot preserves historical availability but is not "
            "the original host."
        )
        return ("archive", "wayback", 60, notes)
    if "duke-energy.com" in host:
        return ("utility", "duke_live", 80, notes)
    if label:
        return ("external", label, 50, notes)
    return ("unknown", "unknown", 0, notes)


def _authority_rank(authority: str) -> int:
    normalized = authority.lower()
    return {
        "regulator": 90,
        "utility": 80,
        "archive": 60,
        "external": 50,
        "reference": 10,
    }.get(normalized, 0)


def _source_label_from_notes(notes: list[str]) -> str | None:
    for note in notes:
        if note.startswith("source="):
            return note.split("=", maxsplit=1)[1]
    return None


def _metadata_value(metadata: dict, key: str) -> str | None:
    value = metadata.get(key)
    return str(value) if value not in (None, "") else None


def _safe_json_load(payload: str | None) -> dict:
    if not payload:
        return {}
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    nested = value.get("metadata_json")
    if isinstance(nested, str):
        try:
            nested_value = json.loads(nested)
        except json.JSONDecodeError:
            nested_value = {}
        if isinstance(nested_value, dict):
            merged = dict(nested_value)
            merged.update({k: v for k, v in value.items() if k != "metadata_json"})
            return merged
    return value
