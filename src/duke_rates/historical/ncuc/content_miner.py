from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.historical.ncuc.importer import NcucPipelineImporter
from duke_rates.historical.ncuc.metadata import (
    PRIORITY_SCHEDULE_CODES,
    classify_filing,
    extract_leaf_nos,
    extract_rider_codes,
)
from duke_rates.models.ncuc import NcucDiscoveryRecord, NcucFetchStatus, NcucFilingClassification

logger = logging.getLogger(__name__)

KNOWN_RIDER_CODES = {
    "BA",
    "CAR",
    "CEI",
    "CPRE",
    "DSM",
    "EDIT",
    "EE",
    "ESM",
    "JAA",
    "JAAR",
    "PIM",
    "RDM",
    "RECD",
    "REPS",
    "STS",
}

MONTH_NAME_PAT = re.compile(
    r"\b("
    r"January|February|March|April|May|June|July|August|September|October|November|December"
    r")\s+\d{1,2},\s+\d{4}\b",
    re.IGNORECASE,
)
EFFECTIVE_PAT = re.compile(
    r"(?i)\b(effective(?:\s+for\s+service\s+rendered)?(?:\s+on\s+and\s+after)?|applicable)\b"
)
TARIFF_SIGNAL_PATTERNS = [
    re.compile(r"(?i)\bleaf\s+no\.?"),
    re.compile(r"(?i)\brevised\s+leaf"),
    re.compile(r"(?i)\brate\s+schedule\b"),
    re.compile(r"(?i)\brider\s+(?:no\.?\s*)?[a-z0-9-]+"),
    re.compile(r"(?i)\beffective\s+for\s+service\s+rendered"),
    re.compile(r"(?i)\bper\s+kwh\b"),
    re.compile(r"(?i)\bcustomer\s+charge\b"),
]
PROPOSED_RIDER_PAT = re.compile(r"(?i)\bproposed\s+rider\b")
SUMMARY_RIDER_PAT = re.compile(r"(?i)\bsummary\s+of\b.*\bproposed\s+rider\b")


class NcucPdfContentMiner:
    def __init__(self, settings: Settings, repository: Repository):
        self.settings = settings
        self.repository = repository
        self.importer = NcucPipelineImporter(settings, repository)

    def mine_records(
        self,
        *,
        docket_number: str | None = None,
        family_query: str | None = None,
        limit: int | None = None,
        max_pages: int = 12,
        force: bool = False,
    ) -> list[dict[str, object]]:
        records = self.repository.list_ncuc_discovery_records(docket_number=docket_number)
        if family_query:
            needle = family_query.lower()
            filtered: list[NcucDiscoveryRecord] = []
            for record in records:
                haystacks = [
                    record.docket_number or "",
                    record.filing_title or "",
                    " ".join(record.referenced_schedule_codes),
                    " ".join(record.referenced_rider_codes),
                    " ".join(record.family_keys),
                    record.metadata_json or "",
                ]
                if any(needle in item.lower() for item in haystacks):
                    filtered.append(record)
            records = filtered

        candidates = [
            record
            for record in records
            if record.fetch_status == NcucFetchStatus.SUCCESS
            and record.local_path
            and str(record.local_path).lower().endswith(".pdf")
        ]
        if limit is not None:
            candidates = candidates[:limit]

        summaries: list[dict[str, object]] = []
        for record in candidates:
            summary = self._mine_record(record, max_pages=max_pages, force=force)
            if summary:
                summaries.append(summary)
        return summaries

    def _mine_record(
        self,
        record: NcucDiscoveryRecord,
        *,
        max_pages: int,
        force: bool,
    ) -> dict[str, object] | None:
        pdf_path = Path(record.local_path or "")
        if not pdf_path.exists():
            return None

        text_path = pdf_path.with_suffix(pdf_path.suffix + ".txt")
        text = ""
        if text_path.exists() and not force:
            text = text_path.read_text(encoding="utf-8", errors="ignore")
        else:
            text = _extract_pdf_text(pdf_path, max_pages=max_pages)
            if text:
                text_path.write_text(text, encoding="utf-8")

        if not text.strip():
            return None

        extracted_schedule_codes = _merge_unique(
            _sanitize_schedule_codes(record.referenced_schedule_codes),
            _extract_priority_codes(text),
        )
        extracted_rider_codes = _merge_unique(
            _sanitize_rider_codes(record.referenced_rider_codes),
            _extract_known_rider_codes(text),
        )
        extracted_leaf_nos = _merge_unique(
            record.referenced_leaf_nos,
            extract_leaf_nos(text),
        )
        effective_date = _extract_effective_date(text)
        derived_title = _derive_title_from_text(text)
        inferred_title = record.filing_title
        if _should_replace_title(record.filing_title, derived_title):
            inferred_title = derived_title
        contains_tariff_text = _contains_tariff_text(text)
        filing_classification = (
            NcucFilingClassification.TARIFF_SHEETS
            if contains_tariff_text
            else classify_filing((inferred_title or "") + " " + text[:4000])
        )

        metadata = {}
        if record.metadata_json:
            try:
                metadata = json.loads(record.metadata_json)
            except json.JSONDecodeError:
                metadata = {"raw_metadata_json": record.metadata_json}
        metadata["pdf_content_mining"] = {
            "max_pages": max_pages,
            "contains_tariff_text": contains_tariff_text,
            "effective_date": effective_date,
            "derived_title": derived_title,
            "selected_title": inferred_title,
            "extracted_schedule_codes": extracted_schedule_codes,
            "extracted_rider_codes": extracted_rider_codes,
            "extracted_leaf_nos": extracted_leaf_nos,
            "text_path": str(text_path),
        }

        updated = record.model_copy(
            update={
                "filing_title": inferred_title,
                "filing_date": effective_date or record.filing_date,
                "filing_classification": filing_classification,
                "referenced_schedule_codes": extracted_schedule_codes,
                "referenced_rider_codes": extracted_rider_codes,
                "referenced_leaf_nos": extracted_leaf_nos,
                "metadata_json": json.dumps(metadata, sort_keys=True),
                "provenance_notes": _merge_unique(
                    record.provenance_notes,
                    ["pdf_content_mined"],
                ),
            }
        )
        updated_id = self.repository.upsert_ncuc_discovery_record(updated)
        stored = self.repository.get_ncuc_discovery_record(updated_id) or updated
        import_summary = self.importer.import_discovery_record(stored)
        return {
            "record_id": updated_id,
            "docket_number": stored.docket_number,
            "filing_title": stored.filing_title,
            "effective_date": effective_date,
            "contains_tariff_text": contains_tariff_text,
            "schedule_codes": extracted_schedule_codes,
            "rider_codes": extracted_rider_codes,
            "leaf_nos": extracted_leaf_nos,
            "lead_ids": import_summary["lead_ids"],
            "docket_lead_ids": import_summary["docket_lead_ids"],
            "family_keys_matched": import_summary["family_keys_matched"],
            "text_path": str(text_path),
        }


def _extract_pdf_text(pdf_path: Path, *, max_pages: int) -> str:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - env-specific
        raise RuntimeError("pdfplumber is required for NCUC PDF content mining") from exc

    chunks: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages[:max_pages]:
            text = page.extract_text() or ""
            if text:
                chunks.append(text)
    return "\n".join(chunks)


def _extract_priority_codes(text: str) -> list[str]:
    found: list[str] = []
    for code in PRIORITY_SCHEDULE_CODES:
        if re.search(rf"(?i)\b(?:schedule|rider|leaf|rate schedule)\s*(?:no\.?\s*)?{code}\b", text):
            found.append(code)
    return found


def _extract_known_rider_codes(text: str) -> list[str]:
    codes: list[str] = []
    for code in extract_rider_codes(text):
        if code in KNOWN_RIDER_CODES:
            codes.append(code)
    return codes


def _sanitize_schedule_codes(codes: list[str]) -> list[str]:
    return [code for code in codes if code in PRIORITY_SCHEDULE_CODES]


def _sanitize_rider_codes(codes: list[str]) -> list[str]:
    return [code for code in codes if code in KNOWN_RIDER_CODES]


def _extract_effective_date(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines[:80]:
        if not EFFECTIVE_PAT.search(line):
            continue
        month_match = MONTH_NAME_PAT.search(line)
        if month_match:
            return month_match.group(0)
    for line in lines[:80]:
        month_match = MONTH_NAME_PAT.search(line)
        if month_match:
            return month_match.group(0)
    return None


def _derive_title_from_text(text: str) -> str | None:
    lines = [
        re.sub(r"\s+", " ", line).strip(" :-")
        for line in text.splitlines()
        if line and line.strip()
    ]
    for line in lines[:250]:
        upper = line.upper()
        if SUMMARY_RIDER_PAT.search(line) and len(line) <= 120:
            return line[:240]
        if PROPOSED_RIDER_PAT.search(line) and len(line) <= 120:
            return line[:240]
    for index, line in enumerate(lines[:250]):
        upper = line.upper()
        if (
            len(line) <= 120
            and "RIDER" in upper
            and any(code in upper for code in KNOWN_RIDER_CODES)
        ):
            window = lines[max(0, index - 1): index + 1]
            cleaned = [
                item
                for item in window
                if item
                and "DUKE ENERGY PROGRESS" not in item.upper()
                and "NORTH CAROLINA ONLY" not in item.upper()
                and "LAW OFFICE" not in item.upper()
            ]
            if cleaned:
                return " ".join(cleaned)[:240]
    for index, line in enumerate(lines[:200]):
        upper = line.upper()
        if not ("SCHEDULE" in upper or upper.startswith("RIDER ")):
            continue
        window = lines[max(0, index - 2): index + 1]
        cleaned = [
            item
            for item in window
            if item
            and "DUKE ENERGY PROGRESS" not in item.upper()
            and "NORTH CAROLINA ONLY" not in item.upper()
            and "LAW OFFICE" not in item.upper()
        ]
        if cleaned:
            return " ".join(cleaned)[:240]
    for line in lines[:20]:
        if len(line) < 5:
            continue
        if any(char.isalpha() for char in line):
            return line[:240]
    return None


def _contains_tariff_text(text: str) -> bool:
    return any(pattern.search(text) for pattern in TARIFF_SIGNAL_PATTERNS)


def _should_replace_title(existing_title: str | None, derived_title: str | None) -> bool:
    if not derived_title:
        return False
    if not existing_title:
        return True
    existing_upper = existing_title.upper()
    derived_upper = derived_title.upper()
    if ("SCHEDULE" in derived_upper or derived_upper.startswith("RIDER ")) and not (
        "SCHEDULE" in existing_upper or existing_upper.startswith("RIDER ")
    ):
        return True
    generic_keywords = [
        "LAW OFFICE",
        "STATE OF NORTH CAROLINA",
        "DEPUTY GENERAL COUNSEL",
        "JACK E. JIRAK",
        "KENDRICK C. FENTRESS",
        "LAWRENCE B. SOMERS",
        "BRIAN L. FRANKLIN",
    ]
    return any(keyword in existing_upper for keyword in generic_keywords)


def _merge_unique(*value_lists: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for values in value_lists:
        for value in values:
            if not value:
                continue
            normalized = value.strip()
            if not normalized:
                continue
            key = normalized.upper()
            if key in seen:
                continue
            seen.add(key)
            merged.append(normalized)
    return merged
