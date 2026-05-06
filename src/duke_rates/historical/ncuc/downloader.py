"""NCUC document downloader: fetch PDFs and other source documents."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.download.hashing import sha256_bytes
from duke_rates.historical.ncuc.discovery import NcucDiscoveryService
from duke_rates.models.ncuc import NcucDiscoveryRecord, NcucFetchStatus
from duke_rates.utils.files import ensure_parent
from duke_rates.utils.retry import retry_call
from duke_rates.utils.text import slugify

logger = logging.getLogger(__name__)

NCUC_STORAGE_SUBDIR = "ncuc"


def _build_ncuc_path(base_dir: Path, record: NcucDiscoveryRecord, suffix: str) -> Path:
    """Build a stable archive path for an NCUC document."""
    docket_slug = slugify(record.docket_number or "unknown-docket")
    title_slug = slugify((record.filing_title or "document")[:80])
    date_part = (record.filing_date or "nodate").replace("-", "")[:8]
    filename = f"{docket_slug}-{date_part}-{title_slug}{suffix}"
    return ensure_parent(
        base_dir / "historical" / NCUC_STORAGE_SUBDIR / docket_slug / filename
    )


class NcucDownloader:
    """
    Download NCUC documents to local storage.

    Supports:
    - Direct HTTP download when a stable download URL is known
    - Session cookie preservation for portal downloads
    - Playwright fallback when HTTP is blocked
    - Duplicate avoidance via content hash check
    """

    def __init__(self, settings: Settings, repository: Repository):
        self.settings = settings
        self.repository = repository
        self._client = httpx.Client(
            follow_redirects=True,
            timeout=settings.request_timeout,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "application/pdf,*/*",
            },
        )
        self._discovery = NcucDiscoveryService(settings)

    def close(self) -> None:
        self._client.close()
        self._discovery.close()

    # ------------------------------------------------------------------
    # Primary fetch: try download_url, then attachment_url, then viewer
    # ------------------------------------------------------------------

    def fetch(self, record: NcucDiscoveryRecord) -> NcucDiscoveryRecord:
        """
        Attempt to download the document for a discovery record.
        Returns updated record with fetch_status, local_path, content_hash, etc.
        """
        record_id = record.id

        # Resolve candidate download URL
        download_url = record.download_url or record.attachment_url

        if not download_url and record.viewer_url:
            logger.info("Resolving viewer URL to download URL: %s", record.viewer_url)
            download_url = self._discovery.resolve_viewer_to_download_url(record.viewer_url)
            if download_url:
                record = record.model_copy(update={"download_url": download_url})

        if not download_url:
            logger.warning("No download URL available for record id=%s", record_id)
            updated = record.model_copy(
                update={
                    "fetch_status": NcucFetchStatus.FAILED,
                    "error_detail": "no_download_url_found",
                    "fetched_at": datetime.now(UTC),
                }
            )
            if record_id:
                self.repository.mark_ncuc_fetch_status(
                    record_id,
                    status=NcucFetchStatus.FAILED,
                    error_detail="no_download_url_found",
                )
            return updated

        # Attempt HTTP download
        try:
            content, content_type, final_url = self._http_fetch(download_url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 403:
                logger.info("HTTP 403 on %s - trying Playwright", download_url)
                content, content_type, final_url = self._playwright_fetch(download_url)
            else:
                return self._mark_failed(record, str(exc))
        except Exception as exc:
            return self._mark_failed(record, str(exc))

        if content is None:
            return self._mark_failed(record, "empty_response")

        # Deduplicate by content hash
        content_hash = sha256_bytes(content)
        existing = [
            r
            for r in self.repository.list_ncuc_discovery_records()
            if r.content_hash == content_hash and r.local_path
        ]
        if existing:
            logger.info("Duplicate content hash %s - skipping download", content_hash[:12])
            updated = record.model_copy(
                update={
                    "fetch_status": NcucFetchStatus.SKIPPED_DUPLICATE,
                    "content_hash": content_hash,
                    "local_path": existing[0].local_path,
                    "fetched_at": datetime.now(UTC),
                }
            )
            if record_id:
                self.repository.mark_ncuc_fetch_status(
                    record_id,
                    status=NcucFetchStatus.SKIPPED_DUPLICATE,
                    content_hash=content_hash,
                    local_path=existing[0].local_path,
                )
            return updated

        # Determine file extension
        suffix = Path(urlparse(final_url).path).suffix
        if not suffix:
            suffix = ".pdf" if "pdf" in content_type.lower() else ".bin"

        archive_path = _build_ncuc_path(self.settings.raw_dir, record, suffix)
        archive_path.write_bytes(content)
        logger.info("Downloaded NCUC document → %s", archive_path)

        updated = record.model_copy(
            update={
                "fetch_status": NcucFetchStatus.SUCCESS,
                "local_path": str(archive_path),
                "content_hash": content_hash,
                "content_type": content_type,
                "file_size_bytes": len(content),
                "fetched_at": datetime.now(UTC),
                "download_url": final_url,
            }
        )
        if record_id:
            self.repository.mark_ncuc_fetch_status(
                record_id,
                status=NcucFetchStatus.SUCCESS,
                local_path=str(archive_path),
                content_hash=content_hash,
                content_type=content_type,
                file_size_bytes=len(content),
                fetched_at=datetime.now(UTC),
            )
        return updated

    def fetch_pending(self, *, limit: int = 20) -> list[NcucDiscoveryRecord]:
        """Fetch all pending NCUC discovery records up to limit."""
        pending = self.repository.list_ncuc_discovery_records(
            fetch_status=NcucFetchStatus.PENDING.value
        )
        results = []
        for record in pending[:limit]:
            logger.info("Fetching pending record id=%s: %s", record.id, record.filing_title)
            results.append(self.fetch(record))
        return results

    def retry_failed(self, *, limit: int = 10) -> list[NcucDiscoveryRecord]:
        """Retry previously failed downloads."""
        failed = self.repository.list_ncuc_discovery_records(
            fetch_status=NcucFetchStatus.FAILED.value
        )
        results = []
        for record in failed[:limit]:
            if not record.download_url and not record.viewer_url and not record.attachment_url:
                continue  # nothing to retry
            logger.info("Retrying failed record id=%s", record.id)
            results.append(self.fetch(record))
        return results

    # ------------------------------------------------------------------
    # Internal fetch helpers
    # ------------------------------------------------------------------

    def _http_fetch(self, url: str) -> tuple[bytes, str, str]:
        response = retry_call(
            lambda: self._client.get(url),
            retries=self.settings.max_retries,
            delay_seconds=self.settings.rate_limit_seconds,
            retry_on=(httpx.HTTPError,),
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        return response.content, content_type, str(response.url)

    def _playwright_fetch(self, url: str) -> tuple[bytes | None, str, str]:
        """Download a document using Playwright (for session-gated or JS-rendered pages)."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("Playwright not installed; cannot browser-fetch %s", url)
            return None, "", url

        logger.info("Playwright download: %s", url)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(user_agent=self.settings.user_agent)
            page = context.new_page()

            # Capture PDF responses via network interception
            pdf_content: list[bytes] = []
            pdf_url: list[str] = []
            pdf_type: list[str] = []

            def _on_response(resp):
                ct = resp.headers.get("content-type", "")
                if "pdf" in ct.lower() or resp.url.lower().endswith(".pdf"):
                    try:
                        body = resp.body()
                        pdf_content.append(body)
                        pdf_url.append(resp.url)
                        pdf_type.append(ct)
                    except Exception:
                        pass

            page.on("response", _on_response)
            try:
                page.goto(
                    url,
                    wait_until="networkidle",
                    timeout=int(self.settings.request_timeout * 1000),
                )
            finally:
                browser.close()

        if pdf_content:
            return pdf_content[0], pdf_type[0], pdf_url[0]

        return None, "", url

    def _mark_failed(
        self,
        record: NcucDiscoveryRecord,
        error_detail: str,
    ) -> NcucDiscoveryRecord:
        logger.warning("NCUC fetch failed for id=%s: %s", record.id, error_detail)
        updated = record.model_copy(
            update={
                "fetch_status": NcucFetchStatus.FAILED,
                "error_detail": error_detail,
                "fetched_at": datetime.now(UTC),
            }
        )
        if record.id:
            self.repository.mark_ncuc_fetch_status(
                record.id,
                status=NcucFetchStatus.FAILED,
                error_detail=error_detail,
                fetched_at=datetime.now(UTC),
            )
        return updated
