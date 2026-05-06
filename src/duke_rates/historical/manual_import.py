from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from duke_rates.config import Settings
from duke_rates.db.repository import Repository
from duke_rates.download.hashing import sha256_bytes
from duke_rates.historical.metadata import extract_historical_metadata
from duke_rates.models.document import DocumentCategory, DocumentKind
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.parse.html_extract import extract_html_text
from duke_rates.parse.notice_parser import parse_notice_text
from duke_rates.parse.pdf_text import extract_pdf_text
from duke_rates.parse.rider_parser import parse_rider_text
from duke_rates.parse.schedule_parser import parse_schedule_text
from duke_rates.utils.files import ensure_parent
from duke_rates.utils.retry import retry_call
from duke_rates.utils.text import slugify


class ProgressNCHistoricalImportService:
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
        self.client = httpx.Client(
            follow_redirects=True,
            timeout=settings.request_timeout,
            headers={"User-Agent": settings.user_agent},
        )

    def close(self) -> None:
        self.client.close()

    def import_document(
        self,
        *,
        title: str,
        category: str,
        source_label: str,
        source_authority: str | None = None,
        source_type: str | None = None,
        source_url: str | None = None,
        local_file: Path | None = None,
        docket_number: str | None = None,
        family_key_override: str | None = None,
        parse_text_override: str | None = None,
        parse_text_metadata: dict[str, object] | None = None,
    ) -> HistoricalDocumentRecord:
        kind = _infer_kind(source_url=source_url, local_file=local_file)
        content, content_type, canonical_url, archived_url = self._load_content(
            source_url=source_url,
            local_file=local_file,
            kind=kind,
        )
        archive_path = self._build_archive_path(
            title=title,
            category=category,
            kind=kind,
            source_url=source_url or str(local_file),
        )
        archive_path.write_bytes(content)

        raw_text, raw_text_path = self._extract_text(
            kind=kind,
            archive_path=archive_path,
            parse_text_override=parse_text_override,
        )
        metadata = extract_historical_metadata(raw_text) if raw_text else {}
        canonical_url, archived_url = _logical_source_urls(
            canonical_url=canonical_url,
            archived_url=archived_url,
            family_key_override=family_key_override,
        )
        retrieved_at = datetime.now(UTC)
        record = HistoricalDocumentRecord(
            current_document_id=None,
            family_key=family_key_override or _family_key(canonical_url),
            title=title,
            state=self.state,
            company=self.company,
            category=category,
            kind=kind.value,
            canonical_url=canonical_url,
            archived_url=archived_url,
            snapshot_timestamp=retrieved_at,
            local_path=archive_path,
            raw_text_path=raw_text_path,
            content_hash=sha256_bytes(content),
            content_type=content_type,
            direct_status_code=200 if source_url else None,
            direct_downloadable=bool(source_url),
            revision_label=metadata.get("revision_label"),
            supersedes_label=metadata.get("supersedes_label"),
            leaf_no=metadata.get("leaf_no"),
            effective_start=metadata.get("effective_start"),
            effective_end=metadata.get("effective_end"),
            retrieved_at=retrieved_at,
            metadata_json=json.dumps(
                {
                    "docket_number": docket_number,
                    "family_key_override": family_key_override,
                    "local_file": str(local_file) if local_file else None,
                    "parse_text_metadata": parse_text_metadata or None,
                    "parse_text_override": bool(parse_text_override),
                    "source_authority": source_authority,
                    "source_label": source_label,
                    "source_type": source_type,
                    "source_url": source_url,
                },
                sort_keys=True,
            ),
            notes=[f"source={source_label}", f"jurisdiction={self.state}-{self.company}"],
        )
        historical_id = self.repository.upsert_historical_document(record)
        parse_result = self._parse_document(
            historical_id=historical_id,
            title=title,
            category=category,
            kind=kind,
            raw_text=raw_text,
            raw_text_path=raw_text_path,
        )
        if parse_result:
            self.repository.save_historical_parse_result(historical_id, parse_result)
        stored = self.repository.get_historical_document(historical_id)
        if not stored:
            raise RuntimeError("Imported historical document was not persisted.")
        return stored

    def _load_content(
        self,
        *,
        source_url: str | None,
        local_file: Path | None,
        kind: DocumentKind,
    ) -> tuple[bytes, str | None, str, str]:
        if local_file and local_file.exists():
            content = local_file.read_bytes()
            return (
                content,
                "application/pdf" if kind == DocumentKind.PDF else "text/html",
                str(source_url) if source_url else f"local-file://{local_file.name}",
                str(source_url) if source_url else f"local-file://{local_file.name}",
            )
        if source_url:
            response = retry_call(
                lambda: self.client.get(source_url),
                retries=max(self.settings.max_retries - 1, 0),
                delay_seconds=self.settings.rate_limit_seconds,
                retry_on=(httpx.HTTPError,),
            )
            response.raise_for_status()
            return (
                response.content,
                response.headers.get("content-type"),
                str(response.url),
                str(response.url),
            )
        raise ValueError("Provide source_url or local_file.")

    def _extract_text(
        self,
        *,
        kind: DocumentKind,
        archive_path: Path,
        parse_text_override: str | None = None,
    ) -> tuple[str, Path | None]:
        if parse_text_override is not None:
            raw_text = parse_text_override
        elif kind == DocumentKind.PDF:
            raw_text = extract_pdf_text(archive_path)
        elif kind == DocumentKind.HTML:
            raw_text = extract_html_text(archive_path)
        else:
            return ("", None)
        raw_text_path = archive_path.with_suffix(archive_path.suffix + ".txt")
        raw_text_path.write_text(raw_text, encoding="utf-8")
        return (raw_text, raw_text_path)

    def _parse_document(
        self,
        *,
        historical_id: int,
        title: str,
        category: str,
        kind: DocumentKind,
        raw_text: str,
        raw_text_path: Path | None,
    ):
        if kind not in {DocumentKind.PDF, DocumentKind.HTML} or not raw_text_path:
            return None
        if category in {DocumentCategory.PUBLIC_NOTICE.value, DocumentCategory.OTHER.value}:
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

    def _build_archive_path(
        self,
        *,
        title: str,
        category: str,
        kind: DocumentKind,
        source_url: str,
    ) -> Path:
        parsed = urlparse(source_url)
        stem = slugify(f"{title}-{parsed.path or parsed.netloc}-{parsed.query}")
        suffix = ".pdf" if kind == DocumentKind.PDF else ".html"
        return ensure_parent(
            self.settings.historical_dir
            / "raw"
            / self.state.lower()
            / self.company.lower()
            / category
            / f"{stem}{suffix}"
        )


def _infer_kind(*, source_url: str | None, local_file: Path | None) -> DocumentKind:
    if local_file:
        suffix = local_file.suffix.lower()
    elif source_url:
        suffix = Path(urlparse(source_url).path).suffix.lower()
    else:
        raise ValueError("Provide source_url or local_file.")
    if suffix == ".pdf":
        return DocumentKind.PDF
    if suffix in {".html", ".htm"}:
        return DocumentKind.HTML
    return DocumentKind.OTHER


def _family_key(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.path or url).lower()


def _logical_source_urls(
    *,
    canonical_url: str,
    archived_url: str,
    family_key_override: str | None,
) -> tuple[str, str]:
    if not family_key_override:
        return (canonical_url, archived_url)
    if "starw1.ncuc.gov" not in canonical_url and "starw1.ncuc.gov" not in archived_url:
        return (canonical_url, archived_url)
    suffix = f"#family={slugify(family_key_override)}"
    return (f"{canonical_url}{suffix}", f"{archived_url}{suffix}")
