from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlparse

import httpx

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.discovery.duke_site import API_HEADERS, JURISDICTION_SET_URL
from duke_rates.discovery.link_extractor import (
    extract_links_from_html,
    extract_links_from_jss_payload,
    guess_category,
    guess_kind,
)
from duke_rates.download.hashing import sha256_bytes
from duke_rates.historical.metadata import extract_historical_metadata
from duke_rates.historical.wayback import WaybackClient, normalize_archived_target
from duke_rates.models.document import DocumentCategory, DocumentKind
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.parse.notice_parser import parse_notice_text
from duke_rates.parse.pdf_text import extract_pdf_text
from duke_rates.parse.rider_parser import parse_rider_text
from duke_rates.parse.schedule_parser import parse_schedule_text
from duke_rates.utils.dates import utc_now
from duke_rates.utils.files import ensure_parent
from duke_rates.utils.retry import retry_call
from duke_rates.utils.text import slugify

logger = logging.getLogger(__name__)

PUBLIC_NOTICE_URL = "https://www.duke-energy.com/home/billing/rates/public-notices?jur=NC"
PUBLIC_NOTICE_API_URL = (
    "https://www.duke-energy.com/cdxp/api/core/content/jsspublic//home/billing/rates/"
    "public-notices/en/NC02?item=%2Fhome%2Fbilling%2Frates%2Fpublic-notices"
)
SUPPORTED_PARSE_CATEGORIES = {
    DocumentCategory.RATE.value,
    DocumentCategory.RIDER.value,
    DocumentCategory.TARIFF.value,
    DocumentCategory.PUBLIC_NOTICE.value,
    DocumentCategory.OTHER.value,
}


class ProgressNCPublicNoticeRecoveryService:
    def __init__(self, settings: Settings, repository: Repository, *, state: str = "NC", company: str = "progress"):
        self.settings = settings
        self.repository = repository
        self.state = state
        self.company = company
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
        self.wayback.close()
        self.client.close()

    def recover(
        self,
        *,
        from_year: int = 2023,
        max_page_snapshots: int = 8,
        max_documents_per_snapshot: int = 20,
    ) -> list[HistoricalDocumentRecord]:
        recovered: dict[int, HistoricalDocumentRecord] = {}
        for item in self._discover_current_notice_documents()[:max_documents_per_snapshot]:
            try:
                record = self._recover_current_notice_document(item=item)
            except httpx.HTTPError as exc:
                logger.warning(
                    "Current public-notice document fetch failed for %s: %s",
                    item["url"],
                    exc,
                )
                continue
            if record:
                recovered[record.id or len(recovered)] = record

        if recovered:
            return list(recovered.values())

        snapshots = retry_call(
            lambda: self.wayback.lookup_page_snapshots(
                PUBLIC_NOTICE_URL,
                from_year=from_year,
                limit=max_page_snapshots,
            ),
            retries=max(self.settings.max_retries - 1, 0),
            delay_seconds=self.settings.rate_limit_seconds,
            retry_on=(httpx.HTTPError,),
        )
        for snapshot in snapshots:
            try:
                page_response = retry_call(
                    lambda archive_url=snapshot.archive_url: self.client.get(archive_url),
                    retries=max(self.settings.max_retries - 1, 0),
                    delay_seconds=self.settings.rate_limit_seconds,
                    retry_on=(httpx.HTTPError,),
                )
                page_response.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning(
                    "Archived public-notice page fetch failed for %s: %s",
                    snapshot.archive_url,
                    exc,
                )
                continue

            documents = _extract_notice_documents(page_response.text, PUBLIC_NOTICE_URL)
            for item in documents[:max_documents_per_snapshot]:
                if item["kind"] != DocumentKind.PDF.value:
                    continue
                try:
                    record = self._recover_notice_document(
                        item=item,
                        page_archive_url=snapshot.archive_url,
                        page_snapshot_timestamp=snapshot.timestamp,
                    )
                except httpx.HTTPError as exc:
                    logger.warning(
                        "Archived notice document fetch failed for %s: %s",
                        item["url"],
                        exc,
                    )
                    continue
                if record:
                    recovered[record.id or len(recovered)] = record
        return list(recovered.values())

    def _discover_current_notice_documents(self) -> list[dict]:
        session_headers = {
            "User-Agent": self.settings.user_agent,
            **API_HEADERS,
        }
        with httpx.Client(
            follow_redirects=True,
            timeout=self.settings.request_timeout,
            headers=session_headers,
        ) as session:
            session.post(
                JURISDICTION_SET_URL,
                json={"stateAbbreviation": "NC", "serviceKey": "02"},
            ).raise_for_status()
            payload = session.get(PUBLIC_NOTICE_API_URL).json()
        docs, _ = extract_links_from_jss_payload(payload, PUBLIC_NOTICE_URL)
        normalized: dict[str, dict] = {}
        for item in docs:
            canonical_url = normalize_archived_target(str(item["url"]))
            if urlparse(canonical_url).netloc != "www.duke-energy.com":
                continue
            kind = guess_kind(canonical_url)
            if kind != DocumentKind.PDF:
                continue
            title = str(item["title"])
            normalized[canonical_url] = {
                "title": title,
                "url": canonical_url,
                "category": guess_category(title, canonical_url).value,
                "kind": kind.value,
            }
        return list(normalized.values())

    def _recover_current_notice_document(
        self,
        *,
        item: dict,
    ) -> HistoricalDocumentRecord | None:
        canonical_url = str(item["url"])
        response = retry_call(
            lambda document_url=canonical_url: self.client.get(document_url),
            retries=max(self.settings.max_retries - 1, 0),
            delay_seconds=self.settings.rate_limit_seconds,
            retry_on=(httpx.HTTPError,),
        )
        response.raise_for_status()
        if "pdf" not in (response.headers.get("content-type") or "").lower():
            return None

        retrieved_at = utc_now()
        content = response.content
        archive_path = self._build_archive_path(item["title"], item["category"], canonical_url)
        archive_path.write_bytes(content)
        raw_text = extract_pdf_text(archive_path)
        raw_text_path = archive_path.with_suffix(archive_path.suffix + ".txt")
        raw_text_path.write_text(raw_text, encoding="utf-8")

        metadata = extract_historical_metadata(raw_text)
        record = HistoricalDocumentRecord(
            current_document_id=None,
            family_key=_family_key(canonical_url),
            title=item["title"],
            state=self.state,
            company=self.company,
            category=item["category"],
            kind=DocumentKind.PDF.value,
            canonical_url=canonical_url,
            archived_url=canonical_url,
            snapshot_timestamp=retrieved_at,
            local_path=archive_path,
            raw_text_path=raw_text_path,
            content_hash=sha256_bytes(content),
            content_type=response.headers.get("content-type"),
            direct_status_code=response.status_code,
            direct_downloadable=response.status_code == 200,
            revision_label=metadata["revision_label"],
            supersedes_label=metadata["supersedes_label"],
            leaf_no=metadata["leaf_no"],
            effective_start=metadata["effective_start"],
            effective_end=metadata["effective_end"],
            retrieved_at=retrieved_at,
            metadata_json=json.dumps(
                {
                    "source": "current_public_notice_index",
                    "page_url": PUBLIC_NOTICE_URL,
                    "api_url": PUBLIC_NOTICE_API_URL,
                },
                sort_keys=True,
            ),
            notes=["source=current-public-notice-index", "jurisdiction=NC-progress"],
        )
        historical_id = self.repository.upsert_historical_document(record)
        if item["category"] in SUPPORTED_PARSE_CATEGORIES:
            parse_result = self._parse_document(
                historical_id=historical_id,
                title=item["title"],
                category=item["category"],
                raw_text=raw_text,
                raw_text_path=raw_text_path,
            )
            self.repository.save_historical_parse_result(historical_id, parse_result)
        return self.repository.get_historical_document(historical_id)

    def _recover_notice_document(
        self,
        *,
        item: dict,
        page_archive_url: str,
        page_snapshot_timestamp: str,
    ) -> HistoricalDocumentRecord | None:
        canonical_url = normalize_archived_target(item["url"])
        if urlparse(canonical_url).netloc != "www.duke-energy.com":
            return None
        archive_url = (
            item["url"]
            if "web.archive.org/web/" in item["url"]
            else f"https://web.archive.org/web/{page_snapshot_timestamp}/{canonical_url}"
        )
        response = retry_call(
            lambda archive_url=archive_url: self.client.get(archive_url),
            retries=max(self.settings.max_retries - 1, 0),
            delay_seconds=self.settings.rate_limit_seconds,
            retry_on=(httpx.HTTPError,),
        )
        response.raise_for_status()
        if "pdf" not in (response.headers.get("content-type") or "").lower():
            return None

        content = response.content
        archive_path = self._build_archive_path(item["title"], item["category"], archive_url)
        archive_path.write_bytes(content)
        raw_text = extract_pdf_text(archive_path)
        raw_text_path = archive_path.with_suffix(archive_path.suffix + ".txt")
        raw_text_path.write_text(raw_text, encoding="utf-8")

        metadata = extract_historical_metadata(raw_text)
        direct_status_code, direct_downloadable = self._check_direct_download(canonical_url)
        record = HistoricalDocumentRecord(
            current_document_id=None,
            family_key=_family_key(canonical_url),
            title=item["title"],
            state=self.state,
            company=self.company,
            category=item["category"],
            kind=DocumentKind.PDF.value,
            canonical_url=canonical_url,
            archived_url=archive_url,
            snapshot_timestamp=_snapshot_timestamp(page_snapshot_timestamp),
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
            retrieved_at=_snapshot_timestamp(page_snapshot_timestamp),
            metadata_json=json.dumps(
                {
                    "source": "public_notice_wayback",
                    "page_archive_url": page_archive_url,
                    "page_snapshot_timestamp": page_snapshot_timestamp,
                },
                sort_keys=True,
            ),
            notes=["source=public-notice-wayback", "jurisdiction=NC-progress"],
        )
        historical_id = self.repository.upsert_historical_document(record)
        if item["category"] in SUPPORTED_PARSE_CATEGORIES:
            parse_result = self._parse_document(
                historical_id=historical_id,
                title=item["title"],
                category=item["category"],
                raw_text=raw_text,
                raw_text_path=raw_text_path,
            )
            self.repository.save_historical_parse_result(historical_id, parse_result)
        return self.repository.get_historical_document(historical_id)

    def _parse_document(
        self,
        *,
        historical_id: int,
        title: str,
        category: str,
        raw_text: str,
        raw_text_path: Path,
    ):
        if category in {DocumentCategory.PUBLIC_NOTICE.value, DocumentCategory.OTHER.value}:
            return parse_notice_text(
                document_id=historical_id,
                title=title,
                state=self.state,
                company=self.company,
                text=raw_text,
                raw_text_path=raw_text_path,
            )
        if "notice" in title.lower():
            return parse_notice_text(
                document_id=historical_id,
                title=title,
                state=self.state,
                company=self.company,
                text=raw_text,
                raw_text_path=raw_text_path,
            )
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

    def _check_direct_download(self, canonical_url: str) -> tuple[int | None, bool]:
        try:
            response = self.client.get(canonical_url)
        except httpx.HTTPError:
            return None, False
        return response.status_code, (
            response.status_code == 200
            and "pdf" in (response.headers.get("content-type") or "").lower()
        )

    def _build_archive_path(self, title: str, category: str, archive_url: str) -> Path:
        parsed = urlparse(archive_url)
        stem = slugify(f"{title}-{parsed.path}-{parsed.query}")
        return ensure_parent(
            self.settings.historical_dir / "raw" / self.state.lower() / self.company.lower() / category / f"{stem}.pdf"
        )


def _extract_notice_documents(html: str, base_url: str) -> list[dict]:
    documents, _ = extract_links_from_html(html, base_url)
    normalized: dict[str, dict] = {}
    for item in documents:
        original_url = normalize_archived_target(item["url"])
        kind = guess_kind(original_url)
        if kind != DocumentKind.PDF:
            continue
        title = item["title"]
        category = guess_category(title, original_url).value
        normalized[original_url] = {
            "title": title,
            "url": original_url,
            "category": category,
            "kind": kind.value,
        }
    return list(normalized.values())


def _family_key(url: str) -> str:
    return urlparse(url).path.lower()


def _snapshot_timestamp(timestamp: str):
    from datetime import UTC, datetime

    return datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
