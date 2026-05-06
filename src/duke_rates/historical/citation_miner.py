from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.external.openei import OpenEIClient, OpenEIRateReference
from duke_rates.historical.family_targets import (
    ProgressNCFamilyTarget,
    build_progress_nc_family_targets,
)
from duke_rates.historical.lead_scoring import score_historical_lead
from duke_rates.historical.provenance import derive_source_provenance
from duke_rates.models.docket_lead import RegulatoryDocketLeadRecord
from duke_rates.models.document import DocumentCategory
from duke_rates.models.evidence_anchor import EvidenceAnchorRecord
from duke_rates.models.historical_lead import HistoricalLeadRecord
from duke_rates.utils.duke_company import PROGRESS_OPENEI_ALIASES

URL_RE = re.compile(r"https?://[^\s<>\"]+", re.I)
DOCKET_RE = re.compile(r"\bE-\d+\s*,?\s*Sub\s*\d+\b", re.I)
LEAF_RE = re.compile(r"Leaf No\.?\s*([A-Z]*\s*\d+)", re.I)
DATE_RE = re.compile(r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b")

class HistoricalCitationMiner:
    def __init__(self, settings: Settings, repository: Repository, *, state: str = "NC", company: str = "progress"):
        self.settings = settings
        self.repository = repository
        self.state = state
        self.company = company

    def mine_openei_progress_nc(
        self,
        *,
        limit_references: int = 120,
        missing_only: bool = True,
    ) -> list[HistoricalLeadRecord]:
        if not self.settings.openei_api_key:
            raise ValueError("Set DUKE_RATES_OPENEI_API_KEY to mine OpenEI leads.")
        targets = build_progress_nc_family_targets(self.repository, missing_only=missing_only)
        client = OpenEIClient(
            api_key=self.settings.openei_api_key,
            timeout=self.settings.request_timeout,
            user_agent=self.settings.user_agent,
            max_retries=self.settings.max_retries,
            rate_limit_seconds=self.settings.rate_limit_seconds,
        )
        try:
            references = _collect_progress_nc_references(client, limit_references=limit_references)
        finally:
            client.close()

        leads: list[HistoricalLeadRecord] = []
        seen: set[tuple[str, str]] = set()
        for reference in references:
            urls = _extract_urls(reference.source_url)
            lead_text = " ".join(
                filter(
                    None,
                    [
                        reference.name,
                        reference.description,
                        reference.source_url,
                        reference.source_parent_uri,
                    ],
                )
            )
            for target in _match_targets(targets, lead_text=lead_text, urls=urls):
                for url in urls or [reference.source_parent_uri]:
                    if not url:
                        continue
                    key = (target.family_key, url)
                    if key in seen:
                        continue
                    seen.add(key)
                    lead = self._build_lead(
                        target=target,
                        source_class="openei_reference",
                        provenance_class="reference",
                        source_label="openei",
                        source_location=reference.uri or reference.source_parent_uri,
                        source_url=reference.source_parent_uri,
                        extracted_url=url,
                        extracted_title=reference.name,
                        docket_numbers=_extract_dockets(lead_text),
                        leaf_reference=_extract_leafs(lead_text),
                        effective_dates=_extract_dates(lead_text),
                        extraction_method="openei_reference_mining",
                        metadata={
                            "openei_label": reference.label,
                            "openei_uri": reference.uri,
                            "openei_utility": reference.utility,
                            "openei_start_date": reference.start_date,
                            "openei_end_date": reference.end_date,
                            "openei_source_parent_uri": reference.source_parent_uri,
                        },
                    )
                    leads.append(self._persist_lead_with_related_records(lead))
        return leads

    def mine_notice_archive_progress_nc(self) -> list[HistoricalLeadRecord]:
        targets = build_progress_nc_family_targets(self.repository, missing_only=True)
        leads: list[HistoricalLeadRecord] = []
        for document in self.repository.list_documents(state=self.state, company=self.company):
            if document.category != DocumentCategory.PUBLIC_NOTICE.value:
                continue
            text = _read_sidecar_text(document.local_path)
            if not text:
                continue
            leads.extend(
                self._mine_text_blob(
                    targets=targets,
                    text=text,
                    source_class="duke_notice",
                    provenance_class="utility",
                    source_label="duke_notice_archive",
                    source_location=str(document.local_path),
                    source_url=document.source_page_url,
                    extraction_method="notice_text_mining",
                    title=document.title,
                )
            )
        for row in self.repository.list_historical_documents(state=self.state, company=self.company):
            if row.category != DocumentCategory.PUBLIC_NOTICE.value or not row.raw_text_path:
                continue
            if not row.raw_text_path.exists():
                continue
            text = row.raw_text_path.read_text(encoding="utf-8", errors="ignore")
            leads.extend(
                self._mine_text_blob(
                    targets=targets,
                    text=text,
                    source_class="duke_notice",
                    provenance_class="utility",
                    source_label="duke_notice_archive",
                    source_location=str(row.raw_text_path),
                    source_url=row.canonical_url,
                    extraction_method="historical_notice_text_mining",
                    title=row.title,
                )
            )
        return leads

    def mine_imported_documents_progress_nc(self) -> list[HistoricalLeadRecord]:
        targets = build_progress_nc_family_targets(self.repository, missing_only=True)
        leads: list[HistoricalLeadRecord] = []
        for row in self.repository.list_historical_documents(state=self.state, company=self.company):
            provenance = derive_source_provenance(row)
            if provenance.authority not in {"regulator", "external", "reference"}:
                continue
            if not row.raw_text_path or not row.raw_text_path.exists():
                continue
            text = row.raw_text_path.read_text(encoding="utf-8", errors="ignore")
            leads.extend(
                self._mine_text_blob(
                    targets=targets,
                    text=text,
                    source_class="imported_document",
                    provenance_class=provenance.authority,
                    source_label=provenance.source_label or provenance.source_type,
                    source_location=str(row.raw_text_path),
                    source_url=provenance.source_url or row.canonical_url,
                    extraction_method="imported_document_mining",
                    title=row.title,
                    source_type=provenance.source_type,
                )
            )
        return leads

    def ingest_manual_lead(
        self,
        *,
        family_query: str | None,
        source_class: str,
        provenance_class: str,
        source_label: str,
        source_location: str | None = None,
        source_url: str | None = None,
        text: str | None = None,
        title: str | None = None,
        docket_number: str | None = None,
    ) -> list[HistoricalLeadRecord]:
        targets = build_progress_nc_family_targets(self.repository, missing_only=False)
        if family_query:
            filtered = {
                key: value
                for key, value in targets.items()
                if family_query.lower()
                in {
                    (value.leaf_no or "").lower(),
                    (value.code or "").lower(),
                    value.title.lower(),
                    value.family_key.lower(),
                }
            }
            targets = filtered or targets
        blob = " ".join(filter(None, [title, text, source_url, docket_number]))
        return self._mine_text_blob(
            targets=targets,
            text=blob,
            source_class=source_class,
            provenance_class=provenance_class,
            source_label=source_label,
            source_location=source_location,
            source_url=source_url,
            extraction_method="manual_lead_ingest",
            title=title,
            source_type=source_class,
        )

    def _mine_text_blob(
        self,
        *,
        targets: dict[str, ProgressNCFamilyTarget],
        text: str,
        source_class: str,
        provenance_class: str,
        source_label: str,
        source_location: str | None,
        source_url: str | None,
        extraction_method: str,
        title: str | None,
        source_type: str | None = None,
    ) -> list[HistoricalLeadRecord]:
        urls = _extract_urls(text)
        matches = _match_targets(
            targets,
            lead_text=" ".join(filter(None, [title, text])),
            urls=urls,
        )
        leads: list[HistoricalLeadRecord] = []
        for target in matches:
            lead = self._build_lead(
                target=target,
                source_class=source_class,
                provenance_class=provenance_class,
                source_label=source_label,
                source_location=source_location,
                source_url=source_url,
                extracted_url=urls[0] if urls else None,
                extracted_title=title,
                docket_numbers=_extract_dockets(text),
                leaf_reference=_extract_leafs(text),
                effective_dates=_extract_dates(text),
                extraction_method=extraction_method,
                metadata={"source_type": source_type, "url_count": len(urls)},
            )
            leads.append(self._persist_lead_with_related_records(lead))
        return leads

    def _build_lead(
        self,
        *,
        target: ProgressNCFamilyTarget,
        source_class: str,
        provenance_class: str,
        source_label: str,
        source_location: str | None,
        source_url: str | None,
        extracted_url: str | None,
        extracted_title: str | None,
        docket_numbers: list[str],
        leaf_reference: str | None,
        effective_dates: list[str],
        extraction_method: str,
        metadata: dict[str, object],
    ) -> HistoricalLeadRecord:
        parsed = urlparse(extracted_url or "")
        lead = HistoricalLeadRecord(
            family_key=target.family_key,
            target_leaf_no=target.leaf_no,
            target_code=target.code,
            target_title=target.title,
            family_type=target.family_type,
            category=target.category,
            source_class=source_class,
            provenance_class=provenance_class,
            source_label=source_label,
            source_location=source_location,
            source_url=source_url,
            extracted_url=extracted_url,
            extracted_title=extracted_title,
            attachment_url=(
                extracted_url
                if extracted_url and extracted_url.lower().endswith(".pdf")
                else None
            ),
            viewer_url=(
                extracted_url if extracted_url and "view" in extracted_url.lower() else None
            ),
            hostname=parsed.netloc.lower() or None,
            path_fragment=parsed.path or None,
            filename=Path(parsed.path).name or None,
            docket_number=docket_numbers[0] if docket_numbers else None,
            schedule_code=target.code if target.category == "rate" else None,
            rider_code=target.code if target.category == "rider" else None,
            leaf_reference=leaf_reference,
            effective_start=effective_dates[0] if effective_dates else None,
            extraction_method=extraction_method,
            metadata_json=json.dumps(metadata, sort_keys=True),
        )
        score, notes = score_historical_lead(lead)
        lead.confidence_score = score
        lead.score_notes = notes
        if len(effective_dates) > 1:
            lead.effective_end = effective_dates[1]
        return lead

    def _persist_lead_with_related_records(
        self,
        lead: HistoricalLeadRecord,
    ) -> HistoricalLeadRecord:
        lead_id = self.repository.upsert_historical_lead(lead)
        stored = next(
            item
            for item in self.repository.list_historical_leads(family_key=lead.family_key)
            if item.id == lead_id
        )
        if stored.docket_number:
            docket_lead = RegulatoryDocketLeadRecord(
                family_key=stored.family_key,
                docket_number=stored.docket_number,
                utility="Duke Energy Progress",
                proceeding_type=_classify_proceeding_type(
                    stored.extracted_title or stored.target_title
                ),
                referenced_codes=[
                    code
                    for code in [
                        stored.target_code,
                        stored.schedule_code,
                        stored.rider_code,
                    ]
                    if code
                ],
                evidence_source=stored.source_label or stored.source_class,
                evidence_source_type=stored.source_class,
                evidence_source_location=stored.source_location or stored.source_url,
                title=stored.extracted_title or stored.target_title,
                contains_tariff_text=False,
                clue_only=True,
                confidence_score=max(20.0, stored.confidence_score - 10),
                notes=["Derived from historical lead mining."],
                metadata_json=stored.metadata_json,
            )
            self.repository.upsert_regulatory_docket_lead(docket_lead)
        if stored.effective_start:
            anchor = EvidenceAnchorRecord(
                family_key=stored.family_key,
                anchor_type="effective_date",
                anchor_value=stored.effective_start,
                start_date=stored.effective_start,
                end_date=stored.effective_end,
                source_type=stored.source_class,
                source_location=stored.source_location or stored.source_url,
                confidence_score=max(15.0, stored.confidence_score - 15),
                notes=["Derived from historical lead mining."],
                metadata_json=stored.metadata_json,
            )
            self.repository.upsert_evidence_anchor(anchor)
        return stored


def _collect_progress_nc_references(
    client: OpenEIClient,
    *,
    limit_references: int,
) -> list[OpenEIRateReference]:
    rows: dict[str, OpenEIRateReference] = {}
    per_alias_limit = max(limit_references * 3, 100)
    for alias in PROGRESS_OPENEI_ALIASES:
        for row in client.lookup_rates(utility=alias, state="NC", limit=per_alias_limit):
            if row.label and row.label not in rows:
                rows[row.label] = row
    return list(rows.values())[:limit_references]


def _extract_urls(text: str | None) -> list[str]:
    if not text:
        return []
    return [match.rstrip(".,);") for match in URL_RE.findall(text)]


def _extract_dockets(text: str) -> list[str]:
    return list(
        dict.fromkeys(
            match.group(0).replace(" ,", ",").strip()
            for match in DOCKET_RE.finditer(text)
        )
    )


def _extract_leafs(text: str) -> str | None:
    match = LEAF_RE.search(text)
    return " ".join(match.group(1).split()) if match else None


def _extract_dates(text: str) -> list[str]:
    return list(dict.fromkeys(match.group(1) for match in DATE_RE.finditer(text)))[:2]


def _read_sidecar_text(local_path: Path) -> str:
    txt_path = local_path.with_suffix(local_path.suffix + ".txt")
    if not txt_path.exists():
        return ""
    return txt_path.read_text(encoding="utf-8", errors="ignore")


def _match_targets(
    targets: dict[str, ProgressNCFamilyTarget],
    *,
    lead_text: str,
    urls: list[str],
) -> list[ProgressNCFamilyTarget]:
    haystack = " ".join([lead_text, *urls]).upper()
    matches: list[ProgressNCFamilyTarget] = []
    for target in targets.values():
        if target.leaf_no and f"LEAF NO. {target.leaf_no}" in haystack:
            matches.append(target)
            continue
        if target.leaf_no and f"LEAF-NO-{target.leaf_no}" in haystack:
            matches.append(target)
            continue
        if target.code and re.search(
            rf"(?<![A-Z0-9]){re.escape(target.code.upper())}(?![A-Z0-9])",
            haystack,
        ):
            matches.append(target)
            continue
        if target.category == "rider" and target.code:
            url_codes = " ".join(_extract_riderish_codes(url) for url in urls)
            if target.code.upper() in url_codes:
                matches.append(target)
                continue
        if any(alias.upper() in haystack for alias in target.aliases if len(alias) > 4):
            matches.append(target)
    deduped: dict[str, ProgressNCFamilyTarget] = {item.family_key: item for item in matches}
    return list(deduped.values())


def _extract_riderish_codes(url: str) -> str:
    filename = Path(urlparse(url).path).name.lower()
    if "rider-" not in filename:
        return ""
    token = filename.split("rider-", maxsplit=1)[1].split(".", maxsplit=1)[0]
    token = token.replace("-ry1", "").replace("-ry", "")
    return token.upper()


def _classify_proceeding_type(title: str | None) -> str:
    text = (title or "").lower()
    if "order" in text:
        return "order"
    if "notice" in text:
        return "notice"
    if "exhibit" in text or "attachment" in text:
        return "exhibit"
    if "compliance" in text:
        return "compliance_filing"
    return "regulatory_clue"
