"""
Structured portal harvesting for NCUC DocumentsParameterSearch.

Converts authenticated portal search results into the same SearchResult shape
used by the public Zoom-search pipeline so ranking, grouping, and export stay
shared.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime

from duke_rates.config import Settings
from duke_rates.historical.ncuc.document_param_search import (
    DocParamSearchResult,
    DocumentParamSearcher,
)
from duke_rates.historical.ncuc.metadata import normalize_filing_date
from duke_rates.historical.ncuc.portal_metadata_analyzer import (
    has_order_approval_signal,
    has_rate_change_signal,
    has_structural_rate_case_pair,
)
from duke_rates.historical.ncuc.query_builder import QuerySpec
from duke_rates.historical.ncuc.result_harvester import HarvestSession, SearchResult
from duke_rates.historical.ncuc.session import (
    NcucSessionError,
    close_authenticated_context,
    create_authenticated_context,
)

logger = logging.getLogger(__name__)


_DOC_TYPE_TO_FILING_TYPES: dict[str, list[str]] = {
    "tariff": ["TARIFF"],
    "tariff sheet": ["TARIFF"],
    "schedule": ["RATESCED"],
    "rate schedule": ["RATESCED"],
    "rate": ["RATESCED"],
    "rider": ["RATESCED"],
    "order": ["ORDER"],
    "informational": ["INFOFILE"],
    "infofile": ["INFOFILE"],
}


@dataclass(frozen=True)
class PortalSearchSpec:
    company_name: str
    filing_types: tuple[str, ...]
    docket_number: str = ""
    date_after: str = ""
    date_before: str = ""
    max_results: int = 250

    @property
    def query_text(self) -> str:
        type_text = ",".join(self.filing_types)
        parts = [f"company={self.company_name}", f"types={type_text}"]
        if self.docket_number:
            parts.append(f"docket={self.docket_number}")
        if self.date_after:
            parts.append(f"after={self.date_after}")
        if self.date_before:
            parts.append(f"before={self.date_before}")
        return "portal:" + " ".join(parts)

    @property
    def template_name(self) -> str:
        return "portal_structured_search"


class PortalSearchHarvester:
    """Harvest structured portal results and map them to pipeline SearchResult rows."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def is_available(self) -> bool:
        return bool(self.settings.ncid_username and self.settings.ncid_password)

    def harvest(
        self,
        *,
        utility: str | None,
        query_specs: list[QuerySpec],
        doc_types: list[str] | None = None,
        max_results: int = 250,
        broad_run: bool = False,
    ) -> HarvestSession:
        session = HarvestSession()
        if not self.is_available:
            logger.info("PortalSearchHarvester: skipping, NCID credentials are not configured")
            return session

        portal_specs = self._build_portal_specs(
            utility=utility,
            query_specs=query_specs,
            doc_types=doc_types,
            max_results=max_results,
        )
        if not portal_specs:
            return session

        logger.info("PortalSearchHarvester: executing %d structured searches", len(portal_specs))
        try:
            pw, ctx, page = create_authenticated_context(self.settings)
        except NcucSessionError as exc:
            logger.warning("PortalSearchHarvester unavailable: %s", exc)
            return session

        try:
            searcher = DocumentParamSearcher(self.settings)
            for spec in portal_specs:
                query_spec = QuerySpec(
                    query_text=spec.query_text,
                    template_name=spec.template_name,
                    utility_hint=spec.company_name,
                    doc_type_hint=",".join(spec.filing_types),
                    priority=5.0,
                    notes=["source=portal"],
                )
                try:
                    raw_results = searcher.search(
                        page,
                        company_name=spec.company_name,
                        docket_number=spec.docket_number,
                        filing_types=list(spec.filing_types),
                        date_after=spec.date_after,
                        date_before=spec.date_before,
                        max_results=spec.max_results,
                    )
                    converted = [
                        self._convert_result(row, query_spec)
                        for row in raw_results
                    ]
                    converted = self._filter_targeted_results(
                        [row for row in converted if row is not None],
                        query_specs=query_specs,
                        broad_run=broad_run,
                    )
                    session.record_query(
                        query_spec=query_spec,
                        new_results=converted,
                        had_error=False,
                        error_snippet="",
                    )
                except Exception as exc:  # pragma: no cover - live portal behavior
                    logger.warning("Portal search failed for %s: %s", spec.query_text, exc)
                    session.record_query(
                        query_spec=query_spec,
                        new_results=[],
                        had_error=True,
                        error_snippet=str(exc)[:200],
                    )
        finally:
            close_authenticated_context(pw, ctx)

        return session

    def _build_portal_specs(
        self,
        *,
        utility: str | None,
        query_specs: list[QuerySpec],
        doc_types: list[str] | None,
        max_results: int,
    ) -> list[PortalSearchSpec]:
        companies = [utility] if utility else ["Duke Energy Progress", "Duke Energy Carolinas"]
        hinted_doc_types = {
            dt.strip().lower()
            for dt in (doc_types or [])
            if dt and dt.strip()
        }
        if not hinted_doc_types:
            hinted_doc_types = {
                (qs.doc_type_hint or "").strip().lower()
                for qs in query_specs
                if qs.doc_type_hint
            }

        filing_types: set[str] = set()
        for doc_type in hinted_doc_types:
            filing_types.update(_DOC_TYPE_TO_FILING_TYPES.get(doc_type, []))
        if not filing_types:
            filing_types.update(["TARIFF", "RATESCED"])
        if filing_types.intersection({"TARIFF", "RATESCED"}):
            filing_types.add("ORDER")

        ordered_types = tuple(ft for ft in ("TARIFF", "RATESCED", "ORDER", "INFOFILE") if ft in filing_types)
        date_after, date_before = self._extract_date_bounds(query_specs)
        docket_numbers = self._extract_docket_hints(query_specs)

        specs: list[PortalSearchSpec] = []
        if docket_numbers:
            for company in companies:
                for docket_number in docket_numbers:
                    specs.append(
                        PortalSearchSpec(
                            company_name=company,
                            filing_types=ordered_types,
                            docket_number=docket_number,
                            date_after=date_after,
                            date_before=date_before,
                            max_results=max_results,
                        )
                    )
            return specs

        return [
            PortalSearchSpec(
                company_name=company,
                filing_types=ordered_types,
                date_after=date_after,
                date_before=date_before,
                max_results=max_results,
            )
            for company in companies
        ]

    def _convert_result(
        self,
        row: DocParamSearchResult,
        query_spec: QuerySpec,
    ) -> SearchResult | None:
        url = row.document_detail_url or (row.view_file_urls[0] if row.view_file_urls else "")
        if not url:
            return None

        date_filed = normalize_filing_date(row.date_filed) or row.date_filed
        snippet_parts = [row.doc_type, row.company_name]
        if row.docket_number:
            snippet_parts.append(row.docket_number)
        if row.view_file_urls:
            snippet_parts.append(f"files={len(row.view_file_urls)}")

        return SearchResult(
            url=url,
            title=row.description or None,
            snippet=" | ".join(part for part in snippet_parts if part) or None,
            filing_date=date_filed,
            docket_number=row.docket_number or None,
            sub_number=None,
            source_query=query_spec.query_text,
            source_template=query_spec.template_name,
            utility_hint=query_spec.utility_hint,
            doc_type_hint=query_spec.doc_type_hint,
            schedule_code_hint=query_spec.schedule_code_hint,
            rider_code_hint=query_spec.rider_code_hint,
            extracted_schedule_codes=row.extracted_schedule_codes,
            extracted_rider_codes=row.extracted_rider_codes,
            extracted_leaf_nos=[],
            filing_classification=row.filing_classification or "other",
            found_by_queries=[query_spec.query_text],
        )

    def _filter_targeted_results(
        self,
        results: list[SearchResult],
        *,
        query_specs: list[QuerySpec],
        broad_run: bool = False,
    ) -> list[SearchResult]:
        # Broad run (no specific targets) — pass all portal results through
        # and let the scorer rank them. Filtering would discard valid filings
        # whose portal description doesn't explicitly mention a schedule code.
        if broad_run:
            logger.info(
                "PortalSearchHarvester: broad run — skipping target filter, passing %d results",
                len(results),
            )
            return results

        schedule_hints = {
            (qs.schedule_code_hint or "").strip().upper()
            for qs in query_specs
            if qs.schedule_code_hint
        }
        rider_hints = {
            (qs.rider_code_hint or "").strip().upper()
            for qs in query_specs
            if qs.rider_code_hint
        }
        if not schedule_hints and not rider_hints:
            return results

        docket_pair_flags = self._build_docket_pair_map(results)
        filtered = [
            result
            for result in results
            if self._matches_target(
                result,
                schedule_hints=schedule_hints,
                rider_hints=rider_hints,
                docket_pair_flags=docket_pair_flags,
            )
        ]
        logger.info(
            "PortalSearchHarvester: target filtering kept %d/%d structured results",
            len(filtered),
            len(results),
        )
        return filtered

    def _matches_target(
        self,
        result: SearchResult,
        *,
        schedule_hints: set[str],
        rider_hints: set[str],
        docket_pair_flags: dict[str, bool] | None = None,
    ) -> bool:
        combined = " ".join(
            part for part in (result.title, result.snippet, result.url) if part
        )
        found_schedules = {code.upper() for code in result.extracted_schedule_codes}
        found_riders = {code.upper() for code in result.extracted_rider_codes}

        if schedule_hints:
            for hint in schedule_hints:
                if hint in found_schedules:
                    return True
                if re.search(rf"\b(?:schedule|rate\s+schedule)\s*{re.escape(hint)}\b", combined, re.I):
                    return True

        if rider_hints:
            for hint in rider_hints:
                if hint in found_riders:
                    return True
                if re.search(rf"\brider\s+{re.escape(hint)}\b", combined, re.I):
                    return True

        docket_key = (result.docket_number or "").strip().upper()
        if docket_key and docket_pair_flags and docket_pair_flags.get(docket_key):
            if has_order_approval_signal(result.title, result.snippet):
                return True
            if has_rate_change_signal(result.title, result.snippet):
                return True
            if result.filing_classification in {"compliance_tariff", "tariff_sheets"}:
                return True

        return False

    @staticmethod
    def _extract_date_bounds(query_specs: list[QuerySpec]) -> tuple[str, str]:
        date_after = ""
        date_before = ""
        note_re = re.compile(r"^(date_after|date_before)=(.+)$", re.I)

        for query_spec in query_specs:
            for note in query_spec.notes:
                match = note_re.match((note or "").strip())
                if not match:
                    continue
                key = match.group(1).lower()
                value = PortalSearchHarvester._normalize_portal_date(match.group(2).strip())
                if not value:
                    continue
                if key == "date_after":
                    date_after = value
                elif key == "date_before":
                    date_before = value

        return date_after, date_before

    @staticmethod
    def _normalize_portal_date(value: str) -> str:
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(value, fmt).strftime("%m/%d/%Y")
            except ValueError:
                continue
        return ""

    @staticmethod
    def _extract_docket_hints(query_specs: list[QuerySpec]) -> list[str]:
        docket_re = re.compile(r"\b([A-Z]-\d+\s+Sub\s+\d+)\b", re.I)
        note_re = re.compile(r"^neighbor_dockets=(true|false)$", re.I)
        raw_dockets: list[str] = []
        include_neighbors = True

        for query_spec in query_specs:
            raw_dockets.extend(match.group(1) for match in docket_re.finditer(query_spec.query_text or ""))
            for note in query_spec.notes:
                note_text = (note or "").strip()
                if note_text.lower().startswith("docket="):
                    raw_dockets.extend(match.group(1) for match in docket_re.finditer(note_text))
                    continue
                neighbor_match = note_re.match(note_text)
                if neighbor_match:
                    include_neighbors = neighbor_match.group(1).lower() == "true"

        normalized = []
        seen: set[str] = set()
        for docket in raw_dockets:
            canonical = PortalSearchHarvester._normalize_docket_number(docket)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            normalized.append(canonical)

        if not include_neighbors:
            return normalized

        expanded: list[str] = []
        seen_expanded: set[str] = set()
        for docket in normalized:
            for candidate in PortalSearchHarvester._expand_neighbor_dockets(docket):
                if candidate in seen_expanded:
                    continue
                seen_expanded.add(candidate)
                expanded.append(candidate)
        return expanded

    @staticmethod
    def _normalize_docket_number(docket: str) -> str:
        match = re.search(r"\b([A-Z]-\d+)\s+Sub\s+(\d+)\b", docket or "", re.I)
        if not match:
            return ""
        return f"{match.group(1).upper()} Sub {int(match.group(2))}"

    @staticmethod
    def _expand_neighbor_dockets(docket: str) -> list[str]:
        match = re.match(r"^([A-Z]-\d+)\s+Sub\s+(\d+)$", docket, re.I)
        if not match:
            return [docket]
        prefix = match.group(1).upper()
        sub_number = int(match.group(2))
        return [f"{prefix} Sub {candidate}" for candidate in range(max(1, sub_number - 2), sub_number + 3)]

    @staticmethod
    def _build_docket_pair_map(results: list[SearchResult]) -> dict[str, bool]:
        grouped: dict[str, list[tuple[str | None, str | None]]] = {}
        for result in results:
            docket_key = (result.docket_number or "").strip().upper()
            if not docket_key:
                continue
            grouped.setdefault(docket_key, []).append((result.title, result.snippet))
        return {
            docket_key: has_structural_rate_case_pair(entries)
            for docket_key, entries in grouped.items()
        }
