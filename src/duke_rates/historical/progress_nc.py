from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.download.hashing import sha256_bytes
from duke_rates.historical.metadata import extract_historical_metadata
from duke_rates.historical.wayback import WaybackClient
from duke_rates.models.document import DocumentCategory, StoredDocument
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.parse.pdf_text import extract_pdf_text
from duke_rates.parse.rider_parser import parse_rider_text
from duke_rates.parse.schedule_parser import parse_schedule_text
from duke_rates.utils.files import ensure_parent
from duke_rates.utils.retry import retry_call
from duke_rates.utils.text import slugify

LEAF_NUMBER_RE = re.compile(r"leaf-no-(\d+)", re.I)
LOW_VALUE_TITLE_TOKENS = (
    "rates",
    "summary",
    "descriptions",
    "service regulations",
    "standard service voltages",
)
logger = logging.getLogger(__name__)


class ProgressNCHistoricalRecoveryService:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        *,
        state: str = "NC",
        company: str = "progress",
    ):
        self.settings = settings
        self.repository = repository
        self.state = state
        self.company = company
        self.wayback = WaybackClient(
            timeout=settings.request_timeout,
            user_agent=settings.user_agent,
        )
        self.direct_client = httpx.Client(
            follow_redirects=True,
            timeout=settings.request_timeout,
            headers={"User-Agent": settings.user_agent},
        )

    def close(self) -> None:
        self.wayback.close()
        self.direct_client.close()

    def recover(
        self,
        *,
        limit_documents: int = 25,
        from_year: int = 2023,
        max_versions_per_document: int = 10,
        categories: set[str] | None = None,
        target_rider_codes: set[str] | None = None,
        target_leaf_numbers: set[str] | None = None,
    ) -> list[HistoricalDocumentRecord]:
        source_documents = self._seed_documents(
            limit_documents,
            categories=categories,
            target_rider_codes=target_rider_codes,
            target_leaf_numbers=target_leaf_numbers,
        )

        recovered: dict[int, HistoricalDocumentRecord] = {}
        for source_document in source_documents:
            try:
                snapshots = retry_call(
                    lambda document_url=source_document.document_url: (
                        self.wayback.lookup_pdf_revisions(
                            document_url,
                            from_year=from_year,
                            limit=max_versions_per_document,
                        )
                    ),
                    retries=max(self.settings.max_retries - 1, 0),
                    delay_seconds=self.settings.rate_limit_seconds,
                    retry_on=(httpx.HTTPError,),
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "Wayback lookup failed for %s: %s",
                    source_document.document_url,
                    exc,
                )
                continue
            for snapshot in snapshots:
                if snapshot.original_url == source_document.document_url:
                    continue
                try:
                    record = retry_call(
                        lambda seed_document=source_document,
                        archive_url=snapshot.archive_url,
                        original_url=snapshot.original_url: self._recover_snapshot(
                            seed_document,
                            archive_url,
                            original_url,
                        ),
                        retries=max(self.settings.max_retries - 1, 0),
                        delay_seconds=self.settings.rate_limit_seconds,
                        retry_on=(httpx.HTTPError,),
                    )
                except httpx.HTTPError as exc:
                    logger.warning("Archived fetch failed for %s: %s", snapshot.archive_url, exc)
                    continue
                if record:
                    recovered[record.id or len(recovered)] = record
        return list(recovered.values())

    def preview_targets(
        self,
        *,
        limit_documents: int = 25,
        from_year: int = 2023,
        max_versions_per_document: int = 10,
        categories: set[str] | None = None,
        target_rider_codes: set[str] | None = None,
        target_leaf_numbers: set[str] | None = None,
    ) -> list[dict[str, object]]:
        source_documents = self._seed_documents(
            limit_documents,
            categories=categories,
            target_rider_codes=target_rider_codes,
            target_leaf_numbers=target_leaf_numbers,
        )
        preview: list[dict[str, object]] = []
        for source_document in source_documents:
            snapshots = self.wayback.lookup_pdf_revisions(
                source_document.document_url,
                from_year=from_year,
                limit=max_versions_per_document,
            )
            existing_rows = [
                row
                for row in self.repository.list_historical_documents(state=self.state, company=self.company)
                if row.family_key.lower() == self._family_key(source_document.document_url)
            ]
            distinct_revisions = [
                snapshot
                for snapshot in snapshots
                if snapshot.original_url != source_document.document_url
            ]
            preview.append(
                {
                    "document_id": source_document.id,
                    "title": source_document.title,
                    "category": source_document.category,
                    "document_url": source_document.document_url,
                    "rider_code": _rider_code_from_url(source_document.document_url),
                    "existing_historical_versions": len(existing_rows),
                    "wayback_snapshot_count": len(snapshots),
                    "candidate_revision_count": len(distinct_revisions),
                    "candidate_revision_urls": [
                        snapshot.original_url for snapshot in distinct_revisions
                    ],
                }
            )
        return preview

    def _seed_documents(
        self,
        limit_documents: int,
        *,
        categories: set[str] | None = None,
        target_rider_codes: set[str] | None = None,
        target_leaf_numbers: set[str] | None = None,
    ) -> list[StoredDocument]:
        normalized_categories = (
            {category.lower() for category in categories} if categories else None
        )
        normalized_leaf_numbers = (
            {str(leaf).strip() for leaf in target_leaf_numbers if str(leaf).strip()}
            if target_leaf_numbers
            else None
        )
        historical_family_keys = {
            row.family_key.lower()
            for row in self.repository.list_historical_documents(state=self.state, company=self.company)
        }
        candidates = [
            doc
            for doc in self.repository.list_documents(state=self.state, company=self.company)
            if doc.kind == "pdf"
            and doc.category in {DocumentCategory.RATE.value, DocumentCategory.RIDER.value}
            and (
                normalized_categories is None or doc.category.lower() in normalized_categories
            )
            and (
                normalized_leaf_numbers is None
                or str(_leaf_number_from_url(doc.document_url) or "") in normalized_leaf_numbers
            )
        ]
        candidates.sort(
            key=lambda document: _seed_priority_with_context(
                document,
                target_rider_codes=target_rider_codes,
                historical_family_keys=historical_family_keys,
            )
        )
        return candidates[:limit_documents]

    def _recover_snapshot(
        self,
        source_document: StoredDocument,
        archive_url: str,
        canonical_url: str,
    ) -> HistoricalDocumentRecord | None:
        response = self.direct_client.get(archive_url)
        response.raise_for_status()
        if "pdf" not in (response.headers.get("content-type") or "").lower():
            return None

        content = response.content
        archive_path = self._build_archive_path(source_document, archive_url)
        archive_path.write_bytes(content)
        raw_text = extract_pdf_text(archive_path)
        raw_text_path = archive_path.with_suffix(archive_path.suffix + ".txt")
        raw_text_path.write_text(raw_text, encoding="utf-8")

        direct_status_code, direct_downloadable = self._check_direct_download(canonical_url)
        metadata = extract_historical_metadata(raw_text)
        record = HistoricalDocumentRecord(
            current_document_id=source_document.id,
            family_key=self._family_key(source_document.document_url),
            title=source_document.title,
            state=source_document.state,
            company=source_document.company,
            category=source_document.category,
            kind=source_document.kind,
            canonical_url=canonical_url,
            archived_url=archive_url,
            snapshot_timestamp=self._snapshot_timestamp_from_archive_url(archive_url),
            local_path=archive_path,
            raw_text_path=raw_text_path,
            content_hash=sha256_bytes(content),
            content_type=response.headers.get("content-type"),
            direct_status_code=direct_status_code,
            direct_downloadable=direct_downloadable,
            revision_label=metadata["revision_label"],
            supersedes_label=metadata["supersedes_label"],
            leaf_no=metadata["leaf_no"],
            effective_start=metadata["effective_start"],
            effective_end=metadata["effective_end"],
            retrieved_at=self._snapshot_timestamp_from_archive_url(archive_url),
            metadata_json=json.dumps(
                {
                    "current_document_id": source_document.id,
                    "current_document_url": source_document.document_url,
                    "source_local_path": str(source_document.local_path),
                },
                sort_keys=True,
            ),
            notes=["source=wayback", f"jurisdiction={self.state}-{self.company}"],
        )
        historical_id = self.repository.upsert_historical_document(record)
        parse_result = self._parse_historical_document(
            historical_id=historical_id,
            source_document=source_document,
            raw_text=raw_text,
            raw_text_path=raw_text_path,
        )
        self.repository.save_historical_parse_result(historical_id, parse_result)
        stored = self.repository.get_historical_document(historical_id)
        return stored

    def _parse_historical_document(
        self,
        *,
        historical_id: int,
        source_document: StoredDocument,
        raw_text: str,
        raw_text_path: Path,
    ):
        if source_document.category == DocumentCategory.RIDER.value:
            return parse_rider_text(
                document_id=historical_id,
                title=source_document.title,
                state=source_document.state,
                company=source_document.company,
                text=raw_text,
                raw_text_path=raw_text_path,
            )
        return parse_schedule_text(
            document_id=historical_id,
            title=source_document.title,
            state=source_document.state,
            company=source_document.company,
            text=raw_text,
            raw_text_path=raw_text_path,
        )

    def _check_direct_download(self, canonical_url: str) -> tuple[int | None, bool]:
        try:
            response = self.direct_client.get(canonical_url)
        except httpx.HTTPError:
            return None, False
        return response.status_code, (
            response.status_code == 200
            and "pdf" in (response.headers.get("content-type") or "").lower()
        )

    def _build_archive_path(self, source_document: StoredDocument, archive_url: str) -> Path:
        parsed = urlparse(archive_url)
        stem = slugify(f"{source_document.title}-{parsed.path}-{parsed.query}")
        return ensure_parent(
            self.settings.historical_dir
            / "raw"
            / self.state.lower()
            / self.company.lower()
            / source_document.category
            / f"{stem}.pdf"
        )

    @staticmethod
    def _family_key(document_url: str) -> str:
        parsed = urlparse(document_url)
        return parsed.path.lower()

    @staticmethod
    def _snapshot_timestamp_from_archive_url(archive_url: str):
        from datetime import UTC, datetime

        marker = "/web/"
        timestamp = archive_url.split(marker, maxsplit=1)[1].split("/", maxsplit=1)[0]
        return datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def _seed_priority(document: StoredDocument) -> tuple[int, int, str]:
    priority = _seed_priority_with_context(
        document,
        target_rider_codes=None,
        historical_family_keys=set(),
    )
    return (priority[2], _leaf_number_from_url(document.document_url) or 9999, priority[4])


def _seed_priority_with_context(
    document: StoredDocument,
    *,
    target_rider_codes: set[str] | None,
    historical_family_keys: set[str],
) -> tuple[int, int, int, int, str]:
    leaf_number = _leaf_number_from_url(document.document_url)
    leaf_priority = leaf_number if leaf_number is not None else 9999
    title = (document.title or "").lower()
    family_key = urlparse(document.document_url).path.lower()
    rider_code = _rider_code_from_url(document.document_url)
    low_value_penalty = 1 if any(token in title for token in LOW_VALUE_TITLE_TOKENS) else 0
    target_penalty = (
        0
        if (
            target_rider_codes
            and rider_code
            and rider_code.upper() in {code.upper() for code in target_rider_codes}
        )
        else 1
    )
    history_penalty = 1 if family_key in historical_family_keys else 0
    category_penalty = (
        0
        if (
            target_rider_codes and document.category == DocumentCategory.RIDER.value
        )
        else 1
    )
    return (
        target_penalty,
        history_penalty,
        low_value_penalty,
        category_penalty,
        f"{leaf_priority}:{title}",
    )


def _leaf_number_from_url(document_url: str) -> int | None:
    match = LEAF_NUMBER_RE.search(document_url)
    return int(match.group(1)) if match else None


def _rider_code_from_url(document_url: str) -> str | None:
    path = urlparse(document_url).path.lower()
    marker = "rider-"
    if marker not in path:
        return None
    token = path.split(marker, maxsplit=1)[1].split(".", maxsplit=1)[0]
    token = token.replace("-ry1", "").replace("-ry", "")
    return token.upper() if token else None
