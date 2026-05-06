from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from duke_rates.db.repository import Repository
from duke_rates.historical.family_targets import (
    ProgressNCFamilyTarget,
    build_progress_nc_family_targets,
    find_target_by_query,
)
from duke_rates.historical.lead_scoring import score_historical_lead
from duke_rates.models.historical_lead import HistoricalLeadRecord

FAMILY_MATCH_RE = re.compile(
    r"^Family:\s*(?P<label>.+?)\s*->\s*URL:\s*(?P<url>https?://\S+)\s*\(Timestamp:\s*(?P<timestamp>\d{8,14})\)",
    re.IGNORECASE,
)
LABELED_MATCH_RE = re.compile(
    r"^(?P<label>[^-].+?)\s*->\s*(?P<url>https?://\S+)\s*\((?P<timestamp>\d{8,14})\)",
    re.IGNORECASE,
)
GENERIC_MATCH_RE = re.compile(
    r"^URL:\s*(?P<url>https?://\S+)\s*\(Timestamp:\s*(?P<timestamp>\d{8,14})\)",
    re.IGNORECASE,
)

TARIFFISH_PATH_MARKERS = (
    "/aboutenergy/rates/",
    "/pdfs/",
    "/-/media/pdfs/",
    "/_/media/pdfs/",
    "/assets/www/docs/company/",
    "/assets/www/docs/home/",
    "/rates/",
)
NEGATIVE_URL_MARKERS = (
    "masterpiece",
    "standardpoles",
    "standard_poles",
    "fixtures",
    "posts",
    "analysts",
    "impact-report",
    "supplier-diversity",
    "safety",
    "brochure",
    "case-study",
    "meetingagenda",
    "sitevisit",
    "bill-insert",
)
NON_NC_PATH_MARKERS = (
    "/dep-sc/",
    "/electric-sc/",
    "/sc/",
    "-sc-",
    "_sc_",
)
POSITIVE_FILENAME_MARKERS = (
    "schedule",
    "rider",
    "rate",
    "tariff",
    "toud",
    "toue",
    "tou",
    "res",
    "sls",
    "slr",
    "jaa",
    "reps",
    "recd",
    "prepay",
    "solar",
    "storm",
    "sts",
    "edit",
    "dsm",
    "ee",
    "cpre",
    "cei",
    "fuel",
)
# Files are now stored under data/manifests/ (moved from project root).
# The service also checks the project root for backwards compatibility.
DEFAULT_ROOT_URL_LIST_FILES = (
    "matches.txt",
    "more_matches.txt",
)
_MANIFEST_SUBDIR = "data/manifests"


@dataclass(frozen=True)
class RootUrlListEntry:
    source_file: Path
    line_number: int
    family_label: str | None
    url: str
    timestamp: str | None
    raw_line: str


class ProgressNCRootUrlListService:
    def __init__(self, repository: Repository, *, project_root: Path | None = None):
        self.repository = repository
        self.project_root = project_root or Path.cwd()

    def default_files(self) -> list[Path]:
        found: list[Path] = []
        for name in DEFAULT_ROOT_URL_LIST_FILES:
            # Prefer data/manifests/ location; fall back to project root for old layouts
            manifest_path = self.project_root / _MANIFEST_SUBDIR / name
            root_path = self.project_root / name
            if manifest_path.exists():
                found.append(manifest_path)
            elif root_path.exists():
                found.append(root_path)
        return found

    def preview_leads(
        self,
        *,
        file_paths: list[Path] | None = None,
        family_query: str | None = None,
        include_noisy: bool = False,
        missing_only: bool = True,
        limit: int | None = None,
    ) -> list[HistoricalLeadRecord]:
        files = file_paths or self.default_files()
        targets = build_progress_nc_family_targets(self.repository, missing_only=missing_only)
        family_target = (
            find_target_by_query(self.repository, family_query, missing_only=missing_only)
            if family_query
            else None
        )
        leads_by_key: dict[tuple[str, str], HistoricalLeadRecord] = {}

        for entry in self.parse_entries(files):
            target = self._match_target(entry, targets, family_target=family_target)
            if not target:
                continue
            if not include_noisy and not self._looks_useful(entry, target):
                continue
            lead = self._build_lead(entry, target)
            key = (lead.family_key, lead.extracted_url or "")
            existing = leads_by_key.get(key)
            if existing is None or lead.confidence_score > existing.confidence_score:
                leads_by_key[key] = lead

        leads = sorted(
            leads_by_key.values(),
            key=lambda item: (
                item.confidence_score,
                item.target_leaf_no or "",
                item.filename or "",
            ),
            reverse=True,
        )
        if limit is not None:
            leads = leads[:limit]
        return leads

    def import_leads(
        self,
        *,
        file_paths: list[Path] | None = None,
        family_query: str | None = None,
        include_noisy: bool = False,
        missing_only: bool = True,
        min_score: float = 45.0,
        limit: int | None = None,
    ) -> list[HistoricalLeadRecord]:
        leads = self.preview_leads(
            file_paths=file_paths,
            family_query=family_query,
            include_noisy=include_noisy,
            missing_only=missing_only,
            limit=limit,
        )
        stored: list[HistoricalLeadRecord] = []
        for lead in leads:
            if lead.confidence_score < min_score:
                continue
            lead_id = self.repository.upsert_historical_lead(lead)
            stored.append(lead.model_copy(update={"id": lead_id}))
        return stored

    def parse_entries(self, file_paths: list[Path]) -> list[RootUrlListEntry]:
        entries: list[RootUrlListEntry] = []
        for path in file_paths:
            if not path.exists():
                continue
            for line_number, raw_line in enumerate(
                path.read_text(encoding="utf-8", errors="ignore").splitlines(),
                start=1,
            ):
                line = raw_line.strip()
                if not line:
                    continue
                parsed = self._parse_line(path, line_number, line)
                if parsed:
                    entries.append(parsed)
        return entries

    def _parse_line(
        self,
        path: Path,
        line_number: int,
        line: str,
    ) -> RootUrlListEntry | None:
        match = FAMILY_MATCH_RE.match(line)
        if match:
            return RootUrlListEntry(
                source_file=path,
                line_number=line_number,
                family_label=match.group("label").strip(),
                url=match.group("url").rstrip(".,);"),
                timestamp=match.group("timestamp"),
                raw_line=line,
            )
        match = LABELED_MATCH_RE.match(line)
        if match:
            return RootUrlListEntry(
                source_file=path,
                line_number=line_number,
                family_label=match.group("label").strip(),
                url=match.group("url").rstrip(".,);"),
                timestamp=match.group("timestamp"),
                raw_line=line,
            )
        match = GENERIC_MATCH_RE.match(line)
        if match:
            return RootUrlListEntry(
                source_file=path,
                line_number=line_number,
                family_label=None,
                url=match.group("url").rstrip(".,);"),
                timestamp=match.group("timestamp"),
                raw_line=line,
            )
        return None

    def _match_target(
        self,
        entry: RootUrlListEntry,
        targets: dict[str, ProgressNCFamilyTarget],
        *,
        family_target: ProgressNCFamilyTarget | None,
    ) -> ProgressNCFamilyTarget | None:
        if family_target:
            if self._entry_matches_target(entry, family_target):
                return family_target
            return None

        if entry.family_label:
            label_target = self._target_from_family_label(entry.family_label, targets)
            if label_target:
                return label_target

        return self._target_from_url(entry.url, targets)

    def _target_from_family_label(
        self,
        family_label: str,
        targets: dict[str, ProgressNCFamilyTarget],
    ) -> ProgressNCFamilyTarget | None:
        parts = [part.strip() for part in family_label.split("/") if part.strip()]
        leaf_hint = parts[0] if parts else None
        code_hint = parts[1] if len(parts) > 1 else None
        if leaf_hint:
            for target in targets.values():
                if target.leaf_no and target.leaf_no == leaf_hint:
                    return target
        if code_hint:
            normalized_code = self._normalize_code(code_hint)
            for target in targets.values():
                if target.code and self._normalize_code(target.code) == normalized_code:
                    return target
        return None

    def _target_from_url(
        self,
        url: str,
        targets: dict[str, ProgressNCFamilyTarget],
    ) -> ProgressNCFamilyTarget | None:
        parsed = urlparse(url)
        path = parsed.path.lower()
        filename = Path(parsed.path).name.lower()
        filename_norm = self._normalize_text(filename)
        path_norm = self._normalize_text(path)

        best_target: ProgressNCFamilyTarget | None = None
        best_score = 0
        for target in targets.values():
            score = 0
            if target.leaf_no and f"leafno{target.leaf_no}" in path_norm:
                score += 5
            if target.code:
                code_norm = self._normalize_code(target.code)
                if code_norm and code_norm in filename_norm:
                    score += 4
                if code_norm and code_norm in path_norm:
                    score += 3
            if score > best_score:
                best_score = score
                best_target = target
        return best_target if best_score > 0 else None

    def _entry_matches_target(
        self,
        entry: RootUrlListEntry,
        target: ProgressNCFamilyTarget,
    ) -> bool:
        if entry.family_label:
            family_target = self._target_from_family_label(
                entry.family_label,
                {target.family_key: target},
            )
            if family_target:
                return True
        matched = self._target_from_url(entry.url, {target.family_key: target})
        return matched is not None

    def _looks_useful(
        self,
        entry: RootUrlListEntry,
        target: ProgressNCFamilyTarget,
    ) -> bool:
        parsed = urlparse(entry.url)
        host = (parsed.hostname or parsed.netloc).lower()
        path = parsed.path.lower()
        filename = Path(parsed.path).name.lower()
        normalized = self._normalize_text(f"{host} {path}")

        if not (host.endswith("duke-energy.com") or host.endswith("progress-energy.com")):
            return False
        if not path.endswith(".pdf"):
            return False
        if any(marker in path for marker in NON_NC_PATH_MARKERS) or any(
            marker in filename for marker in NON_NC_PATH_MARKERS
        ):
            if "nc" not in filename and "nc" not in path:
                return False
        if any(marker in normalized for marker in NEGATIVE_URL_MARKERS):
            return False
        if any(marker in path for marker in TARIFFISH_PATH_MARKERS):
            if entry.family_label:
                return True
            if target.code and self._normalize_code(target.code) in self._normalize_text(filename):
                return True
            if target.leaf_no and target.leaf_no in filename:
                return True
            return False
        if any(marker in filename for marker in POSITIVE_FILENAME_MARKERS):
            return True
        if target.code and self._normalize_code(target.code) in self._normalize_text(filename):
            return True
        if target.leaf_no and target.leaf_no in filename:
            return True
        return False

    def _build_lead(
        self,
        entry: RootUrlListEntry,
        target: ProgressNCFamilyTarget,
    ) -> HistoricalLeadRecord:
        parsed = urlparse(entry.url)
        metadata = {
            "root_url_list": {
                "source_file": str(entry.source_file),
                "line_number": entry.line_number,
                "family_label": entry.family_label,
                "timestamp": entry.timestamp,
                "raw_line": entry.raw_line,
            }
        }
        lead = HistoricalLeadRecord(
            family_key=target.family_key,
            target_leaf_no=target.leaf_no,
            target_code=target.code,
            target_title=target.title,
            family_type=target.family_type,
            category=target.category,
            source_class="root_url_list",
            provenance_class="reference",
            source_label=entry.source_file.stem,
            source_location=str(entry.source_file),
            extracted_url=entry.url,
            extracted_title=entry.family_label or Path(parsed.path).name,
            attachment_url=entry.url,
            hostname=((parsed.hostname or parsed.netloc).lower() or None),
            path_fragment=parsed.path or None,
            filename=Path(parsed.path).name or None,
            schedule_code=target.code if target.category == "rate" else None,
            rider_code=target.code if target.category == "rider" else None,
            leaf_reference=target.leaf_no,
            extraction_method="root_url_list_import",
            metadata_json=json.dumps(metadata, sort_keys=True),
            notes=[
                f"source_file={entry.source_file.name}",
                f"line_number={entry.line_number}",
                *([f"wayback_timestamp={entry.timestamp}"] if entry.timestamp else []),
            ],
        )
        score, notes = score_historical_lead(lead)
        if entry.family_label:
            score += 8
            notes = [*notes, "family label in source list"]
        lead.confidence_score = round(score, 2)
        lead.score_notes = notes
        return lead

    @staticmethod
    def _normalize_code(value: str) -> str:
        return re.sub(r"[^A-Z0-9]+", "", value.upper()).replace("FUEL", "")

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.lower())
