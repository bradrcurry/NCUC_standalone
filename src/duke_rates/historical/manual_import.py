from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from duke_rates.config import Settings
from duke_rates.db.artifact_cache import save_page_artifacts, save_span_artifacts
from duke_rates.db.repository import Repository
from duke_rates.download.hashing import sha256_bytes
from duke_rates.historical.metadata import extract_historical_metadata
from duke_rates.historical.ncuc.pipeline.page_miner import mine_document_pages
from duke_rates.historical.ncuc.pipeline.segmentation import segment_document
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

logger = logging.getLogger(__name__)

# Family-key patterns used to pick a representative span for a one-doc-per-file
# import. Mirrors scripts/maintenance/link_orphan_spans_nc.py; kept inline to
# avoid a new shared module just for two callers.
_LEAF_FAMILY_RES = [
    re.compile(r"-leaf-(\d+)\b", re.IGNORECASE),
    re.compile(r"^ncuc-[a-z]+-(\d+)$", re.IGNORECASE),
]
_RIDER_FAMILY_RE = re.compile(r"-rider-([A-Z0-9]+)\b", re.IGNORECASE)
_SCHED_FAMILY_RE = re.compile(r"-schedule-([A-Z0-9]+)\b", re.IGNORECASE)


def _normalize_code(code: str) -> str:
    return re.sub(r"[\s\-_]", "", code).upper()


def _pick_span_for_manual_import(family_key: str, leaf_no: str | None, spans):
    """Pick the best span for a single-doc-per-file import.

    Returns (start_page, end_page, rule_name) or (None, None, rule_name).
    Mirrors the linker selection rules so the same docs land on the same span
    whether they're imported fresh today or backfilled.
    """
    if not spans:
        return None, None, "no_spans"
    if len(spans) == 1:
        s = spans[0]
        return s.start_page, s.end_page, "single_span"

    target_leaf = (leaf_no or "").strip()
    if not target_leaf and family_key:
        for pat in _LEAF_FAMILY_RES:
            m = pat.search(family_key)
            if m:
                target_leaf = m.group(1)
                break

    target_rider = None
    target_schedule = None
    if family_key:
        m = _RIDER_FAMILY_RE.search(family_key)
        if m:
            target_rider = _normalize_code(m.group(1))
        m = _SCHED_FAMILY_RE.search(family_key)
        if m:
            target_schedule = _normalize_code(m.group(1))

    def _sort_key(s):
        # tariff doc_type first, then highest confidence, then largest span
        return (
            (getattr(s, "doc_type", "") or "") != "tariff",
            -float(getattr(s, "confidence", 0.0) or 0.0),
            -(s.end_page - s.start_page),
        )

    if target_leaf:
        matches = [s for s in spans if target_leaf in [str(x) for x in (getattr(s, "extracted_leaf_nos", []) or [])]]
        if matches:
            matches.sort(key=_sort_key)
            return matches[0].start_page, matches[0].end_page, "leaf_match"

    def _titles_upper(s):
        return [str(t).upper() for t in (getattr(s, "extracted_schedule_titles", []) or [])]

    if target_rider:
        matches = [s for s in spans if any(target_rider in _normalize_code(t) for t in _titles_upper(s))]
        if matches:
            matches.sort(key=_sort_key)
            return matches[0].start_page, matches[0].end_page, "rider_match"

    if target_schedule:
        matches = [s for s in spans if any(target_schedule in _normalize_code(t) for t in _titles_upper(s))]
        if matches:
            matches.sort(key=_sort_key)
            return matches[0].start_page, matches[0].end_page, "schedule_match"

    # Conservative fallback: only auto-pick when the doc is clearly a
    # single-section sheet (<=5 spans). Compliance books stay unlinked for
    # manual review, matching link_orphan_spans_nc.py.
    tariff_spans = [s for s in spans if getattr(s, "doc_type", "") == "tariff"]
    if tariff_spans and len(spans) <= 5:
        tariff_spans.sort(key=_sort_key)
        return tariff_spans[0].start_page, tariff_spans[0].end_page, "best_tariff_span"
    if tariff_spans:
        return None, None, "compliance_book_no_match"
    return None, None, "ambiguous_no_match"


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
        resolved_family_key = family_key_override or _family_key(canonical_url)
        resolved_leaf_no = metadata.get("leaf_no")

        # Compute spans for PDFs so the historical_document is born with a
        # bounded start/end_page, the same way the NCUC ingest path does it.
        # Without this step, single-doc-per-file imports leave start_page NULL
        # and downstream parser profiles see the whole document. See the
        # 2026-05-16 link_orphan_spans_nc.py backfill for the prior fix.
        span_start_page: int | None = None
        span_end_page: int | None = None
        span_selection_rule = "skipped_non_pdf"
        computed_spans = []
        if kind == DocumentKind.PDF:
            try:
                pages = mine_document_pages(str(archive_path))
            except Exception as exc:
                logger.warning("manual_import: page miner failed for %s: %s",
                               archive_path, exc)
                pages = []
            if pages:
                try:
                    computed_spans = segment_document(pages, parent_discovery_id=None)
                except Exception as exc:
                    logger.warning("manual_import: segmentation failed for %s: %s",
                                   archive_path, exc)
                    computed_spans = []
                span_start_page, span_end_page, span_selection_rule = (
                    _pick_span_for_manual_import(
                        resolved_family_key, resolved_leaf_no, computed_spans
                    )
                )
            else:
                span_selection_rule = "no_pages"

        record = HistoricalDocumentRecord(
            current_document_id=None,
            family_key=resolved_family_key,
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
            leaf_no=resolved_leaf_no,
            effective_start=metadata.get("effective_start"),
            effective_end=metadata.get("effective_end"),
            start_page=span_start_page,
            end_page=span_end_page,
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

        # Persist page + span artifacts when we computed them. Downstream
        # workflows (reprocess queue, near-miss audit, parse-improvement loop)
        # all read from ncuc_page_artifacts / ncuc_span_artifacts whether or
        # not this doc came from the NCUC ingest path.
        if computed_spans or (kind == DocumentKind.PDF and pages):
            cache_conn = self.repository._connect()
            try:
                save_page_artifacts(
                    cache_conn,
                    discovery_record_id=None,
                    source_pdf=str(archive_path),
                    file_hash=record.content_hash,
                    pages=pages,
                    metadata={"source_backend": "manual_import"},
                )
                if computed_spans:
                    save_span_artifacts(
                        cache_conn,
                        discovery_record_id=None,
                        source_pdf=str(archive_path),
                        file_hash=record.content_hash,
                        spans=computed_spans,
                        metadata={
                            "source_backend": "manual_import",
                            "selection_rule": span_selection_rule,
                        },
                    )
                cache_conn.commit()
            except Exception as exc:
                logger.warning(
                    "manual_import: failed to persist span/page artifacts for %s: %s",
                    archive_path, exc,
                )
            finally:
                cache_conn.close()

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
