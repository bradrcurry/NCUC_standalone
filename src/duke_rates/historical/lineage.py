from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

from duke_rates.db.repository import Repository
from duke_rates.historical.metadata import extract_historical_metadata
from duke_rates.models.document import DocumentCategory, StoredDocument
from duke_rates.models.history_chain import HistoryChain, HistoryVersion
from duke_rates.models.parse_result import DocumentParseResult
from duke_rates.parse.normalization import parse_effective_date
from duke_rates.parse.pdf_text import extract_pdf_text
from duke_rates.utils.text import normalize_whitespace

SUPPORTED_CATEGORIES = {DocumentCategory.RATE.value, DocumentCategory.RIDER.value}


class ProgressNCLineageService:
    def __init__(
        self,
        repository: Repository,
        *,
        state: str = "NC",
        company: str = "progress",
    ):
        self.repository = repository
        self.state = state
        self.company = company

    def build_chains(
        self,
        *,
        query: str | None = None,
        recovered_only: bool = False,
    ) -> list[HistoryChain]:
        chains: dict[str, list[HistoryVersion]] = defaultdict(list)
        for document in self.repository.list_documents(state=self.state, company=self.company):
            version = self._current_version(document)
            if version:
                chains[version.family_key].append(version)

        for historical in self.repository.list_historical_documents(state=self.state, company=self.company):
            version = self._historical_version(historical)
            chains[version.family_key].append(version)

        built: list[HistoryChain] = []
        normalized_query = query.lower() if query else None
        for versions in chains.values():
            deduped = self._dedupe_versions(versions)
            if recovered_only and not any(
                version.source_kind == "historical" for version in deduped
            ):
                continue
            deduped.sort(key=_history_version_sort_key, reverse=True)
            chain = HistoryChain(
                family_key=deduped[0].family_key,
                title=_chain_title(deduped),
                category=deduped[0].category,
                state=deduped[0].state,
                company=deduped[0].company,
                leaf_no=_chain_leaf_no(deduped),
                versions=deduped,
            )
            if normalized_query and not _chain_matches(chain, normalized_query):
                continue
            built.append(chain)

        built.sort(key=_chain_sort_key)
        return built

    def _current_version(self, document: StoredDocument) -> HistoryVersion | None:
        if document.kind != "pdf" or document.category not in SUPPORTED_CATEGORIES:
            return None

        raw_text = _load_current_text(document.local_path)
        metadata = extract_historical_metadata(raw_text) if raw_text else {}
        parse_result = self.repository.latest_parse_result(document.id)
        tariff_id, schedule_code, rider_id, parsed_effective_start, parsed_effective_end = (
            _parsed_identity(parse_result)
        )
        family_key = _family_key(document.document_url)
        leaf_no = metadata.get("leaf_no") or _leaf_from_family_key(family_key)
        effective_start = metadata.get("effective_start") or parsed_effective_start
        effective_end = metadata.get("effective_end") or parsed_effective_end
        return HistoryVersion(
            source_kind="current",
            document_id=document.id,
            current_document_id=document.id,
            family_key=family_key,
            title=document.title,
            state=document.state,
            company=document.company,
            category=document.category,
            kind=document.kind,
            leaf_no=leaf_no,
            revision_label=metadata.get("revision_label"),
            supersedes_label=metadata.get("supersedes_label"),
            effective_start=effective_start,
            effective_end=effective_end,
            tariff_id=tariff_id,
            schedule_code=schedule_code,
            rider_id=rider_id,
            source_url=document.document_url,
            archived_url=None,
            local_path=document.local_path,
            direct_downloadable=document.status_code == 200,
            retrieved_at=document.retrieved_at,
        )

    @staticmethod
    def _historical_version(historical) -> HistoryVersion:
        tariff_id, schedule_code, rider_id, parsed_effective_start, parsed_effective_end = (
            _parsed_identity_from_json(historical.parsed_result_json)
        )
        return HistoryVersion(
            source_kind="historical",
            document_id=historical.id or 0,
            current_document_id=historical.current_document_id,
            family_key=historical.family_key,
            title=historical.title,
            state=historical.state,
            company=historical.company,
            category=historical.category,
            kind=historical.kind,
            leaf_no=historical.leaf_no,
            revision_label=historical.revision_label,
            supersedes_label=historical.supersedes_label,
            effective_start=historical.effective_start or parsed_effective_start,
            effective_end=historical.effective_end or parsed_effective_end,
            tariff_id=tariff_id,
            schedule_code=schedule_code,
            rider_id=rider_id,
            source_url=historical.canonical_url,
            archived_url=historical.archived_url,
            local_path=historical.local_path,
            direct_downloadable=historical.direct_downloadable,
            retrieved_at=historical.retrieved_at,
        )

    @staticmethod
    def _dedupe_versions(versions: list[HistoryVersion]) -> list[HistoryVersion]:
        deduped: dict[tuple[str | None, ...], HistoryVersion] = {}
        for version in versions:
            key = (
                version.family_key,
                version.revision_label,
                version.effective_start,
                version.effective_end,
                version.schedule_code,
                version.rider_id,
                version.source_kind if version.source_kind == "current" else None,
            )
            existing = deduped.get(key)
            if existing is None or _history_version_sort_key(version) > _history_version_sort_key(
                existing
            ):
                deduped[key] = version
        return list(deduped.values())


def _chain_matches(chain: HistoryChain, query: str) -> bool:
    fields = [
        chain.family_key,
        chain.title,
        chain.leaf_no or "",
    ]
    fields.extend(
        " ".join(
            filter(None, [version.schedule_code, version.rider_id, version.revision_label])
        )
        for version in chain.versions
    )
    haystack = normalize_whitespace(" ".join(str(field) for field in fields if field)).lower()
    return query in haystack


def _chain_sort_key(chain: HistoryChain) -> tuple[str, str]:
    latest = chain.versions[0] if chain.versions else None
    latest_effective = latest.effective_start if latest and latest.effective_start else ""
    return (latest_effective, chain.title.lower())


def _chain_title(versions: list[HistoryVersion]) -> str:
    current = next((version for version in versions if version.source_kind == "current"), None)
    return current.title if current else versions[0].title


def _chain_leaf_no(versions: list[HistoryVersion]) -> str | None:
    for version in versions:
        if version.leaf_no:
            return version.leaf_no
    return None


def _family_key(document_url: str) -> str:
    parsed = urlparse(document_url)
    path = parsed.path if parsed.scheme else document_url
    return path.split("?", maxsplit=1)[0].replace("\\", "/").lower()


def _history_version_sort_key(version: HistoryVersion) -> tuple[int, object, int, object]:
    effective = parse_effective_date(version.effective_start)
    return (
        1 if effective else 0,
        effective or version.retrieved_at.date(),
        1 if version.source_kind == "current" else 0,
        version.retrieved_at,
    )


def _load_current_text(local_path: Path) -> str:
    if not local_path.name or str(local_path) in {"", "."}:
        return ""
    text_path = local_path.with_suffix(local_path.suffix + ".txt")
    if text_path.exists():
        return text_path.read_text(encoding="utf-8")
    if local_path.exists():
        try:
            return extract_pdf_text(local_path)
        except RuntimeError:
            return ""
    return ""


def _parsed_identity(
    parse_result: DocumentParseResult | None,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    if not parse_result:
        return (None, None, None, None, None)
    if parse_result.schedule:
        return (
            parse_result.schedule.tariff_id,
            parse_result.schedule.schedule_code,
            None,
            parse_result.schedule.effective_start.isoformat()
            if parse_result.schedule.effective_start
            else None,
            parse_result.schedule.effective_end.isoformat()
            if parse_result.schedule.effective_end
            else None,
        )
    if parse_result.rider:
        return (
            parse_result.rider.rider_id,
            None,
            parse_result.rider.code,
            parse_result.rider.effective_date,
            None,
        )
    return (None, None, None, None, None)


def _parsed_identity_from_json(
    parsed_result_json: str | None,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    if not parsed_result_json:
        return (None, None, None, None, None)
    return _parsed_identity(DocumentParseResult.model_validate_json(parsed_result_json))


def _leaf_from_family_key(family_key: str) -> str | None:
    token = "leaf-no-"
    if token not in family_key:
        return None
    suffix = family_key.split(token, maxsplit=1)[1]
    return suffix.split("-", maxsplit=1)[0].upper()
