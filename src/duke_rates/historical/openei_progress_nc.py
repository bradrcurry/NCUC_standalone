from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.download.hashing import sha256_bytes
from duke_rates.external.openei import OpenEIClient, OpenEIRateReference
from duke_rates.historical.metadata import extract_historical_metadata
from duke_rates.historical.wayback import WaybackClient, WaybackSnapshot
from duke_rates.models.document import DocumentCategory, DocumentKind
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.parse.heuristics import extract_rider_title, extract_schedule_title
from duke_rates.parse.pdf_text import extract_pdf_text
from duke_rates.parse.rider_parser import parse_rider_text
from duke_rates.parse.schedule_parser import parse_schedule_text
from duke_rates.utils.duke_company import PROGRESS_OPENEI_ALIASES, is_duke_company_related
from duke_rates.utils.files import ensure_parent
from duke_rates.utils.retry import retry_call
from duke_rates.utils.text import slugify

logger = logging.getLogger(__name__)
URL_RE = re.compile(r"https?://[^\s\]\|]+", re.I)


@dataclass(frozen=True)
class OpenEIProgressNCCandidate:
    title: str
    reference: OpenEIRateReference
    source_url: str
    category: str
    family_key: str
    start_date: str | None
    end_date: str | None


class ProgressNCOpenEIHistoricalRecoveryService:
    def __init__(self, settings: Settings, repository: Repository, *, state: str = "NC", company: str = "progress"):
        if not settings.openei_api_key:
            raise ValueError("Set DUKE_RATES_OPENEI_API_KEY to query OpenEI.")
        self.settings = settings
        self.repository = repository
        self.state = state
        self.company = company
        self.openei = OpenEIClient(
            api_key=settings.openei_api_key,
            timeout=settings.request_timeout,
            user_agent=settings.user_agent,
            max_retries=settings.max_retries,
            rate_limit_seconds=settings.rate_limit_seconds,
        )
        self.wayback = WaybackClient(
            timeout=settings.request_timeout,
            user_agent=settings.user_agent,
        )
        self.client = httpx.Client(
            follow_redirects=True,
            timeout=settings.request_timeout,
            headers={"User-Agent": settings.user_agent},
        )

    def close(self) -> None:
        self.openei.close()
        self.wayback.close()
        self.client.close()

    def preview(
        self,
        *,
        limit_references: int = 50,
        target_keys: set[str] | None = None,
    ) -> list[dict[str, object]]:
        preview: list[dict[str, object]] = []
        for row in self._collect_candidates(
            limit_references=limit_references,
            target_keys=target_keys,
        ):
            preview.append(
                {
                    "label": row.reference.label,
                    "title": row.title,
                    "utility": row.reference.utility,
                    "category": row.category,
                    "source_url": row.source_url,
                    "source_parent_uri": row.reference.source_parent_uri,
                    "effective_start": row.start_date,
                    "effective_end": row.end_date,
                    "family_key": row.family_key,
                    "target_keys": sorted(
                        _candidate_target_keys(row.reference, row.source_url, row.title)
                    ),
                }
            )
        return preview

    def recover(
        self,
        *,
        limit_references: int = 50,
        from_year: int = 2010,
        max_wayback_snapshots: int = 8,
        target_keys: set[str] | None = None,
    ) -> list[HistoricalDocumentRecord]:
        recovered: dict[int, HistoricalDocumentRecord] = {}
        for candidate in self._collect_candidates(
            limit_references=limit_references,
            target_keys=target_keys,
        ):
            record = self._recover_candidate(
                candidate,
                from_year=from_year,
                max_wayback_snapshots=max_wayback_snapshots,
            )
            if record:
                recovered[record.id or len(recovered)] = record
        return list(recovered.values())

    def _collect_candidates(
        self,
        *,
        limit_references: int,
        target_keys: set[str] | None = None,
    ) -> list[OpenEIProgressNCCandidate]:
        references = _collect_progress_nc_references(
            self.openei,
            limit_references=limit_references,
        )
        normalized_target_keys = {key.upper() for key in target_keys or set()}
        candidates: list[OpenEIProgressNCCandidate] = []
        seen_urls: set[str] = set()
        for reference in references:
            source_urls = _extract_source_urls(reference.source_url)
            for source_url in source_urls:
                if not _looks_like_progress_nc_reference(reference, source_url):
                    continue
                normalized_url = _normalize_url(source_url)
                if normalized_url in seen_urls:
                    continue
                candidate_keys = _candidate_target_keys(
                    reference,
                    normalized_url,
                    reference.name or "",
                )
                if normalized_target_keys and not (candidate_keys & normalized_target_keys):
                    continue
                seen_urls.add(normalized_url)
                category = _infer_category(reference=reference, source_url=normalized_url)
                title = _candidate_title(
                    reference=reference,
                    source_url=normalized_url,
                    category=category,
                    source_url_count=len(source_urls),
                )
                candidate_keys = _candidate_target_keys(reference, normalized_url, title)
                if normalized_target_keys and not (candidate_keys & normalized_target_keys):
                    continue
                candidates.append(
                    OpenEIProgressNCCandidate(
                        title=title,
                        reference=reference,
                        source_url=normalized_url,
                        category=category,
                        family_key=_family_key(normalized_url),
                        start_date=reference.start_date,
                        end_date=reference.end_date,
                    )
                )
        candidates.sort(
            key=lambda item: (
                item.start_date or "",
                item.title.lower(),
                item.source_url,
            )
        )
        return candidates[:limit_references]

    def _recover_candidate(
        self,
        candidate: OpenEIProgressNCCandidate,
        *,
        from_year: int,
        max_wayback_snapshots: int,
    ) -> HistoricalDocumentRecord | None:
        direct = self._fetch_pdf(candidate.source_url)
        if direct:
            content, content_type = direct
            fetched_url = candidate.source_url
            snapshot_timestamp = datetime.now(UTC)
            direct_downloadable = True
            direct_status_code = 200
        else:
            snapshots = self.wayback.lookup_capture_history(
                candidate.source_url,
                from_year=from_year,
                limit=max_wayback_snapshots,
            )
            snapshots = _rank_snapshots(
                snapshots,
                start_date=candidate.start_date,
                end_date=candidate.end_date,
            )
            content = None
            content_type = None
            fetched_url = None
            snapshot_timestamp = None
            direct_downloadable = False
            direct_status_code = None
            for snapshot in snapshots:
                archived = self._fetch_pdf(snapshot.archive_url)
                if not archived:
                    continue
                content, content_type = archived
                fetched_url = snapshot.archive_url
                snapshot_timestamp = _snapshot_datetime(snapshot)
                break
            if (
                content is None
                or content_type is None
                or fetched_url is None
                or snapshot_timestamp is None
            ):
                return None

        archive_path = self._build_archive_path(candidate, fetched_url)
        archive_path.write_bytes(content)
        raw_text = extract_pdf_text(archive_path)
        raw_text_path = archive_path.with_suffix(archive_path.suffix + ".txt")
        raw_text_path.write_text(raw_text, encoding="utf-8")

        metadata = extract_historical_metadata(raw_text)
        effective_start = metadata.get("effective_start") or candidate.start_date
        effective_end = metadata.get("effective_end") or candidate.end_date
        normalized_title = (
            extract_rider_title(candidate.title, raw_text)
            if candidate.category == DocumentCategory.RIDER.value
            else extract_schedule_title(candidate.title, raw_text)
        )
        record = HistoricalDocumentRecord(
            current_document_id=None,
            family_key=candidate.family_key,
            title=normalized_title,
            state=self.state,
            company=self.company,
            category=candidate.category,
            kind=DocumentKind.PDF.value,
            canonical_url=candidate.source_url,
            archived_url=fetched_url,
            snapshot_timestamp=snapshot_timestamp,
            local_path=archive_path,
            raw_text_path=raw_text_path,
            content_hash=sha256_bytes(content),
            content_type=content_type,
            direct_status_code=direct_status_code,
            direct_downloadable=direct_downloadable,
            revision_label=metadata.get("revision_label"),
            supersedes_label=metadata.get("supersedes_label") or candidate.reference.supercedes,
            leaf_no=metadata.get("leaf_no"),
            effective_start=effective_start,
            effective_end=effective_end,
            retrieved_at=datetime.now(UTC),
            metadata_json=json.dumps(
                {
                    "reference_source": "openei",
                    "openei_label": candidate.reference.label,
                    "openei_name": candidate.reference.name,
                    "openei_utility": candidate.reference.utility,
                    "openei_uri": candidate.reference.uri,
                    "openei_source_parent_uri": candidate.reference.source_parent_uri,
                    "openei_description": candidate.reference.description,
                    "openei_start_date": candidate.reference.start_date,
                    "openei_end_date": candidate.reference.end_date,
                    "openei_supercedes": candidate.reference.supercedes,
                },
                sort_keys=True,
            ),
            notes=["reference=openei", "jurisdiction=NC-progress"],
        )
        historical_id = self.repository.upsert_historical_document(record)
        parse_result = self._parse_document(
            historical_id=historical_id,
            title=normalized_title,
            category=candidate.category,
            raw_text=raw_text,
            raw_text_path=raw_text_path,
        )
        self.repository.save_historical_parse_result(historical_id, parse_result)
        return self.repository.get_historical_document(historical_id)

    def _fetch_pdf(self, url: str) -> tuple[bytes, str | None] | None:
        try:
            response = retry_call(
                lambda: self.client.get(url),
                retries=max(self.settings.max_retries - 1, 0),
                delay_seconds=self.settings.rate_limit_seconds,
                retry_on=(httpx.HTTPError,),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.debug("OpenEI historical fetch failed for %s: %s", url, exc)
            return None
        content_type = response.headers.get("content-type")
        if not _is_pdf_payload(response.content, content_type):
            return None
        return (response.content, content_type)

    def _parse_document(
        self,
        *,
        historical_id: int,
        title: str,
        category: str,
        raw_text: str,
        raw_text_path: Path,
    ):
        if category == DocumentCategory.RIDER.value:
            return parse_rider_text(
                document_id=historical_id,
                title=title,
                state=self.state,
                company=self.company,
                text=raw_text,
                raw_text_path=raw_text_path,
            )
        return parse_schedule_text(
            document_id=historical_id,
            title=title,
            state=self.state,
            company=self.company,
            text=raw_text,
            raw_text_path=raw_text_path,
        )

    def _build_archive_path(self, candidate: OpenEIProgressNCCandidate, fetched_url: str) -> Path:
        parsed = urlparse(fetched_url)
        stamp = candidate.start_date or "undated"
        stem = slugify(f"{candidate.title}-{candidate.reference.label}-{stamp}-{parsed.path}")
        return ensure_parent(
            self.settings.historical_dir
            / "raw"
            / self.state.lower()
            / self.company.lower()
            / candidate.category
            / f"{stem}.pdf"
        )


def _collect_progress_nc_references(
    client: OpenEIClient,
    *,
    limit_references: int,
) -> list[OpenEIRateReference]:
    references: dict[str, OpenEIRateReference] = {}
    per_alias_limit = max(limit_references * 3, 100)
    for alias in PROGRESS_OPENEI_ALIASES:
        rows = client.lookup_rates(utility=alias, state="NC", limit=per_alias_limit)
        for row in rows:
            if row.label and row.label not in references:
                references[row.label] = row
    return list(references.values())


def _extract_source_urls(source_value: str | None) -> list[str]:
    if not source_value:
        return []
    return [match.rstrip(".,);") for match in URL_RE.findall(source_value)]


def _normalize_url(url: str) -> str:
    normalized = url.strip()
    if normalized.startswith("http://www.duke-energy.com/"):
        normalized = "https://" + normalized.removeprefix("http://")
    return normalized


def _looks_like_progress_nc_reference(reference: OpenEIRateReference, source_url: str) -> bool:
    utility = reference.utility or ""
    if not is_duke_company_related(utility, "progress"):
        return False
    parsed = urlparse(source_url)
    if "duke-energy.com" not in parsed.netloc.lower():
        return False
    path = parsed.path.lower()
    if not path.endswith(".pdf"):
        return False
    parent = (reference.source_parent_uri or "").lower()
    haystack = " ".join(
        filter(
            None,
            [
                path,
                parent,
                (reference.name or "").lower(),
                (reference.description or "").lower(),
            ],
        )
    )
    if "south carolina" in haystack or "/dep-sc/" in haystack:
        return False
    return any(
        token in haystack
        for token in (
            "north carolina",
            "progress-north-carolina",
            "ncschedule",
            "-nc-",
            "_nc_",
            "/dep-nc/",
            "schedule-res-dep",
            "schedule-r-tou",
        )
    )


def _infer_category(*, reference: OpenEIRateReference, source_url: str) -> str:
    haystack = " ".join(
        filter(
            None,
            [
                (reference.name or "").lower(),
                source_url.lower(),
            ],
        )
    )
    if "rider" in haystack or "/rider-" in haystack:
        return DocumentCategory.RIDER.value
    return DocumentCategory.RATE.value


def _candidate_title(
    *,
    reference: OpenEIRateReference,
    source_url: str,
    category: str,
    source_url_count: int,
) -> str:
    reference_name = (reference.name or "").strip()
    if category == DocumentCategory.RIDER.value and "rider" not in reference_name.lower():
        return _title_from_url(source_url)
    if source_url_count > 1:
        return _title_from_url(source_url)
    return reference_name or _title_from_url(source_url)


def _candidate_target_keys(
    reference: OpenEIRateReference,
    source_url: str,
    title: str,
) -> set[str]:
    haystack = " ".join(
        filter(
            None,
            [
                title,
                reference.name or "",
                source_url,
            ],
        )
    ).upper()
    keys: set[str] = set()
    known_codes = {
        "RES",
        "R-TOUD",
        "R-TOUE",
        "R-TOU",
        "R-TOU-CPP",
        "R-TOU-EV",
        "SLS",
        "SLR",
        "BA",
        "JAA",
        "EDIT-4",
        "CPRE",
        "STS",
        "STS-2",
        "RDM",
        "ESM",
        "PIM",
        "CAR",
        "RECD",
        "EPPWP",
        "RSC",
        "CEI",
    }
    for code in known_codes:
        if re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", haystack):
            keys.add(code)
    alias_rules = {
        "R1-NC-SCHEDULE-RES": "RES",
        "R2-NC-SCHEDULE-R-TOUD": "R-TOUD",
        "R3-NC-SCHEDULE-R-TOUE": "R-TOU",
        "R3-NC-SCHEDULE-R-TOU": "R-TOU",
        "RR1-NC-RIDER-BA": "BA",
    }
    for token, code in alias_rules.items():
        if token in haystack:
            keys.add(code)
    if "ALL ENERGY TIME OF USE" in haystack:
        keys.add("R-TOU")
    return keys


def _title_from_url(url: str) -> str:
    stem = Path(urlparse(url).path).stem
    title = re.sub(r"[-_]+", " ", stem)
    return re.sub(r"\s+", " ", title).strip()


def _family_key(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.path or url).lower()


def _is_pdf_payload(content: bytes, content_type: str | None) -> bool:
    if content.startswith(b"%PDF"):
        return True
    return "pdf" in (content_type or "").lower()


def _snapshot_datetime(snapshot: WaybackSnapshot) -> datetime:
    return datetime.strptime(snapshot.timestamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def _rank_snapshots(
    snapshots: list[WaybackSnapshot],
    *,
    start_date: str | None,
    end_date: str | None,
) -> list[WaybackSnapshot]:
    target = _target_datetime(start_date=start_date, end_date=end_date)
    if not target:
        return sorted(snapshots, key=lambda item: item.timestamp, reverse=True)

    def _rank(snapshot: WaybackSnapshot) -> tuple[int, float, str]:
        capture = _snapshot_datetime(snapshot)
        is_before = 0 if capture >= target else 1
        delta_seconds = abs((capture - target).total_seconds())
        return (is_before, delta_seconds, snapshot.timestamp)

    return sorted(snapshots, key=_rank)


def _target_datetime(*, start_date: str | None, end_date: str | None) -> datetime | None:
    for value in (start_date, end_date):
        if not value:
            continue
        try:
            return datetime.fromisoformat(value).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None
