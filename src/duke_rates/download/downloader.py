from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

import httpx

from duke_rates.config import Settings
from duke_rates.download.hashing import sha256_bytes
from duke_rates.download.storage import build_archive_path
from duke_rates.models.document import DiscoveryRecord, DocumentKind
from duke_rates.utils.retry import retry_call

logger = logging.getLogger(__name__)


class DocumentDownloader:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.Client(
            follow_redirects=True,
            timeout=settings.request_timeout,
            headers={"User-Agent": settings.user_agent},
        )

    def close(self) -> None:
        self.client.close()

    def download(self, record: DiscoveryRecord) -> DiscoveryRecord:
        response = retry_call(
            lambda: self.client.get(str(record.document_url)),
            retries=self.settings.max_retries,
            delay_seconds=self.settings.rate_limit_seconds,
            retry_on=(httpx.HTTPError,),
        )
        response.raise_for_status()
        content = response.content
        content_type = response.headers.get("content-type", "")

        kind = record.kind
        if kind == DocumentKind.OTHER:
            if "pdf" in content_type.lower():
                kind = DocumentKind.PDF
            elif "html" in content_type.lower():
                kind = DocumentKind.HTML

        suffix = Path(urlparse(str(response.url)).path).suffix
        if not suffix:
            suffix = ".pdf" if kind == DocumentKind.PDF else ".html"
        archive_path = build_archive_path(self.settings.raw_dir, record, suffix=suffix)
        archive_path.write_bytes(content)

        logger.info("Downloaded %s -> %s", record.document_url, archive_path)
        return record.model_copy(
            update={
                "kind": kind,
                "local_path": str(archive_path),
                "content_hash": sha256_bytes(content),
                "content_type": content_type,
                "status_code": response.status_code,
            }
        )
