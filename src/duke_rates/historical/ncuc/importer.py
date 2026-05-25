"""
NCUC pipeline importer: bridge between NCUC discovery records and the
existing historical lead / docket / evidence framework.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from duke_rates.config import Settings
from duke_rates.db.artifact_cache import (
    load_page_artifacts,
    load_span_artifacts,
    save_page_artifacts,
    save_span_artifacts,
)
from duke_rates.db.repository import Repository
from duke_rates.historical.family_targets import build_progress_nc_family_targets
from duke_rates.historical.lead_scoring import score_docket_lead
from duke_rates.historical.ncuc.metadata import (
    PRIORITY_SCHEDULE_CODES,
    classify_filing,
    extract_docket_from_text,
    extract_leaf_nos,
    extract_rider_codes,
    extract_schedule_codes,
    score_relevance,
)
from duke_rates.models.docket_lead import RegulatoryDocketLeadRecord
from duke_rates.models.historical_lead import HistoricalLeadRecord
from duke_rates.models.ncuc import NcucDiscoveryRecord, NcucFetchStatus
from duke_rates.models.historical import HistoricalDocumentRecord
from duke_rates.historical.ncuc.pipeline.triage import triage_pdf
from duke_rates.historical.ncuc.pipeline.page_miner import mine_document_pages
from duke_rates.historical.ncuc.pipeline.ocr import (
    extract_ocr_document_pages,
    extract_pages_with_progressive_ocr,
    load_ocr_sidecar_payload,
    summarize_ocr_payload,
)
from duke_rates.historical.ncuc.pipeline.ocr_normalization import normalize_ocr_text
from duke_rates.historical.ncuc.pipeline.segmentation import segment_document
from duke_rates.historical.ncuc.pipeline.family_matcher import (
    classify_span_against_families,
    find_best_family_for_span,
)
from duke_rates.classification import record_classification
from duke_rates.historical.ncuc.pipeline.metadata_extractor import extract_dates_from_span
from duke_rates.historical.ncuc.pipeline.stage_versions import (
    OCR_BACKEND_VERSION,
    OCR_NORMALIZATION_VERSION,
)
from duke_rates.models.pipeline import PipelineRoute
from duke_rates.utils.duke_company import detect_duke_company, normalize_duke_company

logger = logging.getLogger(__name__)

# How we label NCUC sources inside the existing provenance framework
NCUC_SOURCE_CLASS = "ncuc_edocket"
NCUC_SOURCE_AUTHORITY = "regulator"
NCUC_PROVENANCE_CLASS = "regulator"
_PROVISIONAL_TITLE_BLACKLIST = {
    "CERTIFICATE OF SERVICE",
    "TARIFF FOR RIDER",
    "S COMPLIANCE TARIFF FOR RIDER",
    # Generic service/schedule headings that are never extractable schedule names
    "CHARACTER OF SERVICE",
    "COST OF SERVICE",
    "GENERAL SERVICE",
    "LARGE GENERAL SERVICE",
    "INDUSTRIAL SERVICE",
    "RESALE SERVICE",
    "TIME OF USE SERVICE",
    "TRAFFIC SIGNAL SERVICE",
    "DISTRIBUTION AND SERVICE",
    "RATE BASE",
    "RATE CLASS",
    "RATE DESIGN",
    "RATE PERIOD",
    "SELECTION OF RATE SCHEDULE",
    "BILLS UNDER THIS SCHEDULE",
    "DENIAL OR DISCONTINUANCE OF SERVICE",
    "WHEN UNAUTHORIZED USE OF ELECTRIC SERVICE",
    "METERS FOR ALL RESIDENTIAL SERVICE",
    "METERS FOR ALL RESIDENLIAL SERVICE",
}
# Phrases that indicate generic administrative/regulatory text rather than a schedule name.
# Titles ending with these suffixes are procedural language, not schedule headings.
_PROVISIONAL_TITLE_GENERIC_SUFFIXES = (
    " OF SERVICE",
    " AND SERVICE",
    " FOR SERVICE",
    " OF PROGRAM",
    " OF RIDER",
)
# Sentence-fragment prefixes that indicate a title candidate is mid-sentence text,
# not a schedule/rider name. Reject any provisional title that starts with these words.
_PROVISIONAL_TITLE_FRAGMENT_PREFIXES = (
    "AND ", "AS ", "AFTER ", "TO ", "FOR ", "THE ", "OF ", "IN ", "BY ",
    "WITH ", "FROM ", "ON ", "OR ", "IF ", "THAT ", "THIS ", "WHICH ",
    "A ", "AN ", "ALL ", "ANY ", "EACH ", "SUCH ", "SAID ",
)
_EXPLICIT_HEADING_LINE_REGEX = re.compile(
    r"(?i)^\s*((?:SCHEDULE|RIDER|RATE)\s+[A-Z0-9\-]+(?:\s+\([A-Z0-9/\-\s]+\))?)\s*$"
)
_EXPLICIT_HEADING_CODE_REGEX = re.compile(
    r"(?i)^\s*(?:SCHEDULE|RIDER|RATE)\s+([A-Z0-9\-]+)"
)


FAMILY_MATCHING_ALIAS_OVERRIDES: dict[str, tuple[str, ...]] = {
    "nc-carolinas-rider-SCG": (
        "RIDER SG",
        "SG",
        "STANDBY GENERATOR CONTROL",
        "SMALL CUSTOMER GENERATOR",
        "RIDER SG (NC) STANDBY GENERATOR CONTROL",
    ),
    "nc-carolinas-rider-EDPR": (
        "EDPR",
        "EXISTING DSM PROGRAM RIDER",
        "EXISTING DSM PROGRAM COSTS ADJUSTMENT RIDER",
        "EXISTING DSM PROGRAM COSTS ADJUSTMENT RIDER (NC)",
        "DSM PROGRAM COSTS ADJUSTMENT RIDER",
    ),
    "nc-carolinas-rider-BPMPPTTRUEUP": (
        "BPM",
        "BPM RIDER",
        "BPM TRUE-UP RIDER",
        "BPM NET REVENUES AND NON-FIRM POINT-TO-POINT TRANSMISSION REVENUES ADJUSTMENT RIDER",
        "BPM NET REVENUES AND NON-FIRM POINT-TO-POINT TRANSMISSION REVENUES ADJUSTMENT RIDER (NC)",
    ),
}


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        cleaned = (value or "").strip()
        if not cleaned:
            continue
        key = cleaned.upper()
        if key in seen:
            continue
        seen.add(key)
        output.append(cleaned)
    return output


def _build_family_matching_aliases(
    *,
    family_key: str,
    title: str | None,
    code: str | None,
    profile_aliases: list[str] | None = None,
) -> list[str]:
    aliases: list[str] = list(profile_aliases or [])
    if title:
        aliases.append(title)
        for raw_line in str(title).splitlines():
            line = raw_line.strip()
            heading_match = _EXPLICIT_HEADING_LINE_REGEX.match(line)
            if not heading_match:
                continue
            heading = heading_match.group(1).strip()
            aliases.append(heading)
            code_match = _EXPLICIT_HEADING_CODE_REGEX.match(heading)
            if code_match:
                aliases.append(code_match.group(1).upper())
    if code:
        aliases.extend([code, f"RIDER {code}", f"{code} RIDER"])
    aliases.extend(FAMILY_MATCHING_ALIAS_OVERRIDES.get(family_key, ()))
    return _dedupe_strings(aliases)


def _family_type_to_category(family_type: str | None) -> str:
    if family_type == "program":
        return "program"
    if family_type == "rider":
        return "rider"
    return "rate"


def _normalize_identifier_token(text: str) -> str:
    cleaned = re.sub(r"\([^)]*\)", "", text or "").upper()
    cleaned = re.sub(r"[^A-Z0-9]+", "", cleaned)
    return cleaned[:48]


def _looks_like_provisional_title(text: str) -> bool:
    candidate = (text or "").strip()
    if not candidate:
        return False
    upper = re.sub(r"\s+", " ", candidate.upper())
    if upper in _PROVISIONAL_TITLE_BLACKLIST:
        return False
    # Reject mid-sentence fragments (start with conjunctions, prepositions, articles)
    if any(upper.startswith(prefix) for prefix in _PROVISIONAL_TITLE_FRAGMENT_PREFIXES):
        return False
    # Reject titles that start with a digit (addresses) or lowercase (OCR mid-sentence artifacts)
    if candidate and (candidate[0].isdigit() or candidate[0].islower()):
        return False
    # Reject generic administrative phrases that end with service/program/rider
    # (e.g. "Effective November 1, 2013 for service", "Cost of Service")
    if any(upper.endswith(suffix) for suffix in _PROVISIONAL_TITLE_GENERIC_SUFFIXES):
        return False
    return any(token in upper for token in ("RIDER", "SCHEDULE", "SERVICE", "PROGRAM", "RATE"))


def _score_provisional_title_candidate(text: str) -> tuple[int, int, str]:
    candidate = (text or "").strip()
    upper = re.sub(r"\s+", " ", candidate.upper())
    explicit = bool(_EXPLICIT_HEADING_LINE_REGEX.match(candidate))
    generic_penalty = 1 if upper in {"BILLS UNDER THIS SCHEDULE", "GENERAL SERVICE", "INDUSTRIAL SERVICE"} else 0
    return (0 if explicit else 1, generic_penalty, upper)


def _is_generic_provisional_family_key(family_key: str | None) -> bool:
    normalized = (family_key or "").upper()
    return normalized in {
        "NC-PROGRESS-DOC-TYPEOFSERVICE",
        "NC-CAROLINAS-DOC-TYPEOFSERVICE",
        "NC-PROGRESS-DOC-EFFECTIVEFORSERVICE",
        "NC-CAROLINAS-DOC-EFFECTIVEFORSERVICE",
    }


def _is_low_specificity_historical_family(
    *,
    current_document_id: int | None,
    title: str | None,
    code: str | None,
) -> bool:
    if current_document_id:
        return False

    title_upper = re.sub(r"\s+", " ", str(title or "").strip()).upper()
    code_upper = re.sub(r"\s+", " ", str(code or "").strip()).upper()

    if any(_EXPLICIT_HEADING_LINE_REGEX.match(line.strip()) for line in str(title or "").splitlines()):
        return False

    generic_titles = {
        "TYPE OF SERVICE",
        "BILLS UNDER THIS SCHEDULE",
        "GENERAL SERVICE",
        "INDUSTRIAL SERVICE",
        "RESIDENTIAL SERVICE",
        "CHARACTER OF SERVICE",
    }
    generic_prefixes = (
        "EFFECTIVE ",
        "EFFECTIVE FOR SERVICE",
    )

    if title_upper in generic_titles or code_upper in {re.sub(r'[^A-Z0-9]+', '', item) for item in generic_titles}:
        return True
    return any(title_upper.startswith(prefix) for prefix in generic_prefixes)


class NcucPipelineImporter:
    """
    Takes a NcucDiscoveryRecord (downloaded or discovered) and integrates it
    into the project's existing historical_leads, regulatory_docket_leads, and
    evidence_anchors tables.
    """

    def __init__(self, settings: Settings, repository: Repository):
        self.settings = settings
        self.repository = repository

    def import_discovery_record(self, record: NcucDiscoveryRecord) -> dict:
        """
        Import a single NCUC discovery record into the pipeline.

        Returns a summary dict with keys:
            lead_ids, docket_lead_ids, family_keys_matched
        """
        results: dict = {
            "lead_ids": [],
            "docket_lead_ids": [],
            "family_keys_matched": [],
        }

        # Resolve family keys from schedule/rider codes if not already set
        family_keys = list(record.family_keys)
        if not family_keys:
            family_keys = self._resolve_family_keys(record)

        if not family_keys:
            logger.info(
                "No family keys resolved for NCUC record id=%s - importing as unlinked",
                record.id,
            )
            family_keys = ["ncuc-unlinked"]

        results["family_keys_matched"] = family_keys

        # Write resolved family keys back to the discovery record so ncuc family-query works
        real_keys = [fk for fk in family_keys if fk != "ncuc-unlinked"]
        if real_keys and record.id and set(real_keys) != set(record.family_keys):
            updated = record.model_copy(update={"family_keys": real_keys})
            self.repository.upsert_ncuc_discovery_record(updated)

        # Create a historical_lead entry for each matched family
        for fk in family_keys:
            lead_id = self._upsert_historical_lead(record, family_key=fk)
            results["lead_ids"].append(lead_id)

        # Create regulatory_docket_lead entries
        if record.docket_number:
            for fk in family_keys:
                docket_lead_id = self._upsert_docket_lead(record, family_key=fk)
                results["docket_lead_ids"].append(docket_lead_id)

        return results

    def _mark_import_error(self, record_id: int | None, error_detail: str) -> None:
        """Mark a discovery record as handled/skipped by import without changing fetch metadata."""
        if record_id is None:
            return
        try:
            with self.repository._connect() as conn:
                conn.execute(
                    """
                    UPDATE ncuc_discovery_records
                    SET error_detail = ?
                    WHERE id = ?
                    """,
                    (error_detail[:200], record_id),
                )
        except Exception:
            logger.debug("Failed to mark import error for NCUC record id=%s", record_id, exc_info=True)

    def import_all_pending_downloads(
        self,
        *,
        limit: int | None = None,
        max_workers: int = 1,
        max_pages: int = 75,
    ) -> list[dict]:
        """Import successfully-fetched NCUC records that have NOT yet been imported.

        Filters by absence of ncuc_span_artifacts so cycles don't re-process the
        full SUCCESS backlog (~4K records) on every invocation — that scan was
        the cause of the run-continuous-loop-nc 1800s acquisition timeout.
        """
        downloaded = [
            r for r in self.repository.list_ncuc_pending_imports()
            if not str(r.error_detail or "").startswith(("import_skipped_", "import_failed_"))
        ]
        if limit is not None:
            downloaded = downloaded[:max(0, limit)]
        if not downloaded:
            logger.info("import_all_pending_downloads: 0 records pending import")
            return []
        logger.info("import_all_pending_downloads: %d records pending import", len(downloaded))
        import concurrent.futures

        def _process_record(record) -> dict:
            try:
                if max_pages > 0 and record.local_path:
                    triage = triage_pdf(str(record.local_path))
                    page_count = int(getattr(triage, "page_count", 0) or 0)
                    if page_count > max_pages:
                        detail = f"import_skipped_oversized_pdf_pages={page_count}_max={max_pages}"
                        self._mark_import_error(record.id, detail)
                        return {"ncuc_id": record.id, "skipped": detail, "page_count": page_count}
                summary = self.import_discovery_record(record)
                summary["ncuc_id"] = record.id
                
                # Also run the new page-aware mining pipeline
                span_ids = self.mine_discovery_record_spans(record)
                if span_ids:
                    summary["historical_document_ids"] = span_ids
                    
                return summary
            except Exception as exc:
                logger.error("Failed to import NCUC record id=%s: %s", record.id, exc)
                self._mark_import_error(record.id, f"import_failed_{type(exc).__name__}: {exc}")
                return {"ncuc_id": record.id, "error": str(exc)}

        summaries = []
        if max_workers <= 1:
            for record in downloaded:
                summaries.append(_process_record(record))
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(_process_record, r) for r in downloaded]
                for future in concurrent.futures.as_completed(futures):
                    summaries.append(future.result())
                
        return summaries

    def import_all_discovered(self) -> list[dict]:
        """Import all NCUC discovery records (including pending/failed) as leads."""
        all_records = self.repository.list_ncuc_discovery_records()
        import concurrent.futures

        def _process_record(record) -> dict:
            try:
                summary = self.import_discovery_record(record)
                summary["ncuc_id"] = record.id
                return summary
            except Exception as exc:
                logger.error("Failed to import NCUC record id=%s: %s", record.id, exc)
                return {"ncuc_id": record.id, "error": str(exc)}

        summaries = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(_process_record, r) for r in all_records]
            for future in concurrent.futures.as_completed(futures):
                summaries.append(future.result())

        return summaries

    def mine_discovery_record_spans(self, record: NcucDiscoveryRecord) -> list[int]:
        """
        Runs the full page-aware parsing pipeline on a downloaded record's PDF.
        Returns a list of created HistoricalDocumentRecord IDs.
        """
        if not record.local_path or record.fetch_status != NcucFetchStatus.SUCCESS:
            return []
            
        local_path = Path(record.local_path)
        if not local_path.exists():
            return []

        # Stage A: Triage
        triage = triage_pdf(str(local_path))
        if triage.route_recommendation == PipelineRoute.SKIP_IRRELEVANT:
            return []
        file_hash = str(record.content_hash or triage.file_hash or "")

        # Stage B: Page Mining / OCR reintegration with cached artifact reuse.
        cache_conn = self.repository._connect()
        try:
            pages = load_page_artifacts(
                cache_conn,
                source_pdf=str(local_path),
                file_hash=file_hash or None,
            )
        finally:
            cache_conn.close()
        if not pages:
            if triage.route_recommendation == PipelineRoute.OCR_REQUIRED:
                gpu_candidate = getattr(triage, "gpu_ocr_candidate", False)
                pages, selected_backend, attempted_backends = extract_pages_with_progressive_ocr(
                    str(local_path),
                    prefer_gpu=gpu_candidate,
                    table_density=getattr(triage, "keyword_hits", {}).get("table_like_lines", 0) / 100.0,
                    structure_complexity=getattr(triage, "structure_complexity_score", 0.0),
                    document_archetype=getattr(triage, "document_archetype_candidate", None),
                    table_mode=getattr(triage, "table_mode_candidate", None),
                    page_count=getattr(triage, "page_count", 0),
                )
                if not pages:
                    logger.info("OCR-required NCUC record id=%s could not be OCR-mined (attempted: %s)",
                                record.id, attempted_backends)
                    return []
                ocr_summary = summarize_ocr_payload(load_ocr_sidecar_payload(str(local_path)))
                page_metadata = {
                    "artifact_source": "ocr",
                    "triage_confidence_score": getattr(triage, "confidence_score", None),
                    "ocr_confidence_score": getattr(triage, "ocr_confidence_score", None),
                    "native_text_quality_score": getattr(triage, "native_text_quality_score", None),
                    "reading_order_risk_score": getattr(triage, "reading_order_risk_score", None),
                    "gpu_ocr_candidate": getattr(triage, "gpu_ocr_candidate", False),
                    "table_mode_candidate": getattr(triage, "table_mode_candidate", None),
                    "document_archetype_candidate": getattr(triage, "document_archetype_candidate", None),
                    "native_text_backend": getattr(triage, "native_text_backend", None),
                    "ocr_backend_version": OCR_BACKEND_VERSION,
                    "ocr_normalization_version": OCR_NORMALIZATION_VERSION,
                    "ocr_selected_backend": selected_backend,
                    "ocr_attempted_backends": attempted_backends,
                    **ocr_summary,
                }
            else:
                pages = mine_document_pages(str(local_path))
                page_metadata = {
                    "artifact_source": "native_text",
                    "route_recommendation": triage.route_recommendation,
                    "triage_confidence_score": getattr(triage, "confidence_score", None),
                    "ocr_confidence_score": getattr(triage, "ocr_confidence_score", None),
                    "native_text_quality_score": getattr(triage, "native_text_quality_score", None),
                    "reading_order_risk_score": getattr(triage, "reading_order_risk_score", None),
                    "table_mode_candidate": getattr(triage, "table_mode_candidate", None),
                    "document_archetype_candidate": getattr(triage, "document_archetype_candidate", None),
                    "native_text_backend": getattr(triage, "native_text_backend", None),
                }
                # For gpu_ocr_candidate docs with poor native text, try GPU OCR as supplement
                gpu_candidate = getattr(triage, "gpu_ocr_candidate", False)
                native_low_text = pages and (
                    sum(len(p.text_content or "") for p in pages) // max(len(pages), 1) < 80
                )
                if gpu_candidate and native_low_text:
                    logger.info("GPU candidate %s has low native text — trying GPU OCR supplement", record.id)
                    gpu_pages, gpu_backend, gpu_attempted = extract_pages_with_progressive_ocr(
                        str(local_path),
                        prefer_gpu=True,
                        table_density=getattr(triage, "keyword_hits", {}).get("table_like_lines", 0) / 100.0,
                        structure_complexity=getattr(triage, "structure_complexity_score", 0.0),
                        document_archetype=getattr(triage, "document_archetype_candidate", None),
                        table_mode=getattr(triage, "table_mode_candidate", None),
                        page_count=getattr(triage, "page_count", 0),
                    )
                    if gpu_pages and sum(len(p.text_content or "") for p in gpu_pages) > sum(len(p.text_content or "") for p in pages):
                        logger.info("GPU OCR supplement improved text for %s via %s", record.id, gpu_backend)
                        pages = gpu_pages
                        page_metadata = {
                            "artifact_source": "ocr",
                            "route_recommendation": "ocr_required",
                            "triage_confidence_score": getattr(triage, "confidence_score", None),
                            "ocr_confidence_score": getattr(triage, "ocr_confidence_score", None),
                            "native_text_quality_score": getattr(triage, "native_text_quality_score", None),
                            "reading_order_risk_score": getattr(triage, "reading_order_risk_score", None),
                            "gpu_ocr_candidate": True,
                            "table_mode_candidate": getattr(triage, "table_mode_candidate", None),
                            "document_archetype_candidate": getattr(triage, "document_archetype_candidate", None),
                            "native_text_backend": getattr(triage, "native_text_backend", None),
                            "ocr_backend_version": OCR_BACKEND_VERSION,
                            "ocr_normalization_version": OCR_NORMALIZATION_VERSION,
                            "ocr_selected_backend": gpu_backend,
                            "ocr_attempted_backends": gpu_attempted,
                            "escalation_reason": "gpu_candidate_native_low_text",
                        }
            # Normalize page text at storage time — apply once before DB write
            for page in pages:
                if page.text_content:
                    page.text_content = normalize_ocr_text(page.text_content)
            cache_conn = self.repository._connect()
            try:
                save_page_artifacts(
                    cache_conn,
                    discovery_record_id=record.id,
                    source_pdf=str(local_path),
                    file_hash=file_hash or None,
                    pages=pages,
                    metadata=page_metadata,
                )
                cache_conn.commit()
            finally:
                cache_conn.close()
        if not pages:
            return []

        full_text_pages = {p.page_number: p.text_content for p in pages}

        # Stage C: Span Segmentation with cached artifact reuse.
        cache_conn = self.repository._connect()
        try:
            spans = load_span_artifacts(
                cache_conn,
                source_pdf=str(local_path),
                file_hash=file_hash or None,
            )
        finally:
            cache_conn.close()
        if not spans:
            spans = segment_document(pages, parent_discovery_id=record.id)
            if not spans:
                return []
            cache_conn = self.repository._connect()
            try:
                save_span_artifacts(
                    cache_conn,
                    discovery_record_id=record.id,
                    source_pdf=str(local_path),
                    file_hash=file_hash or None,
                    spans=spans,
                    metadata={
                        "route_recommendation": triage.route_recommendation,
                        "triage_confidence_score": getattr(triage, "confidence_score", None),
                        "table_mode_candidate": getattr(triage, "table_mode_candidate", None),
                        "document_archetype_candidate": getattr(triage, "document_archetype_candidate", None),
                        "company_hint": record.utility,
                    },
                )
                cache_conn.commit()
            finally:
                cache_conn.close()
        else:
            for span in spans:
                span.parent_discovery_id = record.id

        # Infer the owning NC utility from the filing metadata. Some older
        # discovery records were stamped with the default DEP utility even when
        # the filing title/path clearly point to Duke Power or Duke Energy
        # Carolinas documents.
        state = "NC"
        company = self._infer_company_from_record(record, pages=pages)

        # Build supported_families from tariff_families table (all 111 leaves) plus
        # aliases from family_search_terms profiles. This is preferred over
        # build_progress_nc_family_targets which only covers the ~22 leaves that have
        # parsed current-version documents.
        from duke_rates.historical.ncuc.family_search_terms import all_profiles
        family_prefix = "nc-progress" if company == "progress" else "nc-carolinas"
        _profile_map = {
            p.leaf: p for p in all_profiles() if p.family_key.startswith(family_prefix)
        }

        supported_families = []
        try:
            conn = self.repository._connect()
            rows = conn.execute(
                "SELECT family_key, title, schedule_code, family_type, current_document_id FROM tariff_families "
                "WHERE family_key LIKE ?",
                (f"{family_prefix}%",),
            ).fetchall()
            for row in rows:
                fk, title, code, ftype, current_document_id = tuple(row)
                if _is_generic_provisional_family_key(fk):
                    continue
                if _is_low_specificity_historical_family(
                    current_document_id=current_document_id,
                    title=title,
                    code=code,
                ):
                    continue
                leaf = fk.split("-")[-1]
                profile = _profile_map.get(leaf)
                aliases = _build_family_matching_aliases(
                    family_key=fk,
                    title=title,
                    code=code,
                    profile_aliases=list(profile.aliases) if profile else [],
                )
                category = _family_type_to_category(ftype)
                supported_families.append({
                    "family_id": fk,
                    "aliases": aliases,
                    "leaf_no": leaf,
                    "code": code,
                    "target_ref": type("T", (), {
                        "family_key": fk,
                        "title": title or "",
                        "leaf_no": leaf,
                        "category": category,
                    })(),
                })
        except Exception as exc:
            logger.warning("Failed to build span-matching families: %s", exc)
            return []

        created_ids = []
        for span in spans:
            self._apply_record_matching_hints(span, record)
            legacy_hint = self._apply_legacy_attachment_matching_hints(span, record)
            # Stage D: Match family
            classification = classify_span_against_families(span, supported_families)
            best_family_key = classification.label if classification else None
            if legacy_hint:
                hinted_family_key = str(legacy_hint.get("family_key") or "")
                if hinted_family_key and any(
                    fam["family_id"] == hinted_family_key for fam in supported_families
                ) and (
                    not best_family_key
                    or best_family_key != hinted_family_key
                    or _is_generic_provisional_family_key(best_family_key)
                ):
                    best_family_key = hinted_family_key
            target = next((f["target_ref"] for f in supported_families if f["family_id"] == best_family_key), None)
            if best_family_key and target and self._should_skip_weak_family_match(span, best_family_key):
                continue
            if not best_family_key or not target:
                if self._should_skip_low_signal_unmatched_span(span):
                    continue
                provisional_target = self._ensure_provisional_family_for_span(
                    state=state,
                    company=company,
                    record=record,
                    span=span,
                )
                if provisional_target is None:
                    continue
                best_family_key = provisional_target.family_key
                target = provisional_target

            # Stage E: Extract Dates
            extract_dates_from_span(span, full_text_pages)
            effective_start = None
            if hasattr(span, 'dates') and span.dates:
                effective_start = span.dates[0].date_value

            from dateutil.parser import parse as parse_date
            try:
                snapshot_dt = parse_date(record.filing_date).isoformat() if record.filing_date else "1970-01-01T00:00:00Z"
            except Exception:
                snapshot_dt = "1970-01-01T00:00:00Z"

            # Stage G: Store into DB
            hist_doc = HistoricalDocumentRecord(
                family_key=best_family_key,
                title=f"{target.title} (Span {span.start_page}-{span.end_page})",
                state=state,
                company=company,
                category=target.category,
                kind="pdf",
                canonical_url=str(record.discovered_url or ""),
                archived_url=f"ncuc://{record.docket_number}/{record.id}#page={span.start_page}",
                snapshot_timestamp=snapshot_dt,
                local_path=local_path,
                content_hash=str(record.content_hash),
                content_type="application/pdf",
                direct_status_code=200,
                direct_downloadable=True,
                leaf_no=target.leaf_no,
                effective_start=effective_start,
                start_page=span.start_page,
                end_page=span.end_page,
                evidence_json=json.dumps(span.evidence_score_breakdown.get(best_family_key, {})),
                retrieved_at=record.fetched_at or datetime.now(UTC).isoformat()
            )
            doc_id = self.repository.upsert_historical_document(hist_doc)
            if doc_id and doc_id not in created_ids:
                created_ids.append(doc_id)

            # Record the family-mapping decision for observability. We persist
            # whether the classifier chose this label OR the legacy_hint
            # overrode it — so disagreement reports can find rows where
            # classifier and hint disagreed.
            if classification and doc_id:
                cls_conn = self.repository._connect()
                try:
                    cls_to_record = classification
                    if best_family_key != classification.label:
                        # Legacy hint overrode the classifier — record both
                        # so we can audit hint-driven overrides.
                        cls_to_record = classification.model_copy(
                            update={
                                "label": best_family_key,
                                "metadata": {
                                    **classification.metadata,
                                    "classifier_label": classification.label,
                                    "classifier_confidence": classification.confidence,
                                    "override_source": "legacy_attachment_hint",
                                },
                            }
                        )
                    record_classification(
                        cls_conn,
                        subject_kind="historical_document",
                        subject_id=str(doc_id),
                        stage="family_mapping",
                        result=cls_to_record,
                    )
                    cls_conn.commit()
                except Exception as exc:
                    logger.debug("Failed to record family_mapping classification: %s", exc)
                finally:
                    cls_conn.close()

        if spans:
            cache_conn = self.repository._connect()
            try:
                save_span_artifacts(
                    cache_conn,
                    discovery_record_id=record.id,
                    source_pdf=str(local_path),
                    file_hash=file_hash or None,
                    spans=spans,
                    metadata={
                        "route_recommendation": triage.route_recommendation,
                        "company_hint": company,
                        "dated_span_cache": True,
                    },
                )
                cache_conn.commit()
            finally:
                cache_conn.close()
        
        return created_ids

    def mine_discovery_record_spans_with_pages(
        self,
        record: NcucDiscoveryRecord,
        pages: list,
        *,
        page_artifact_version: str | None = None,
        page_metadata: dict | None = None,
    ) -> list[int]:
        """
        Variant of mine_discovery_record_spans that skips triage and page mining,
        using precomputed PageEvidence (e.g., from Docling artifacts).

        Runs Stage B save + Stage C segmentation + Stages D–G (family match,
        dates, historical doc creation) unchanged. Caller is responsible for
        running BulkExtractor.process_document() on each returned doc ID.

        Args:
            record: Thin NcucDiscoveryRecord constructed from the source artifact.
                    Must have fetch_status=SUCCESS and a local_path that exists.
            pages: Pre-mined list[PageEvidence] — skips triage and OCR/native mining.
            page_artifact_version: Artifact version tag (e.g. DOCLING_PAGE_MINER_VERSION).
                                   Defaults to PAGE_ARTIFACT_VERSION if not provided.
            page_metadata: Optional metadata dict to pass to save_page_artifacts().

        Returns:
            List of created historical_document IDs (same contract as
            mine_discovery_record_spans).
        """
        from duke_rates.historical.ncuc.pipeline.stage_versions import PAGE_ARTIFACT_VERSION

        if not pages:
            return []

        local_path = Path(record.local_path) if record.local_path else None
        file_hash = str(record.content_hash or "")
        artifact_version = page_artifact_version or PAGE_ARTIFACT_VERSION

        # Stage B: Save pre-mined pages (skip load — caller provides fresh pages)
        cache_conn = self.repository._connect()
        try:
            save_page_artifacts(
                cache_conn,
                discovery_record_id=record.id,
                source_pdf=str(local_path) if local_path else "",
                file_hash=file_hash or None,
                pages=pages,
                metadata=page_metadata or {},
                artifact_version=artifact_version,
            )
            cache_conn.commit()
        finally:
            cache_conn.close()

        full_text_pages = {p.page_number: p.text_content for p in pages}

        # Stage C: Span Segmentation — try cache first, then segment fresh
        cache_conn = self.repository._connect()
        try:
            spans = load_span_artifacts(
                cache_conn,
                source_pdf=str(local_path) if local_path else "",
                file_hash=file_hash or None,
            )
        finally:
            cache_conn.close()
        if not spans:
            spans = segment_document(pages, parent_discovery_id=record.id)
            if not spans:
                return []
            cache_conn = self.repository._connect()
            try:
                save_span_artifacts(
                    cache_conn,
                    discovery_record_id=record.id,
                    source_pdf=str(local_path) if local_path else "",
                    file_hash=file_hash or None,
                    spans=spans,
                    metadata={
                        "source_backend": (page_metadata or {}).get("source_backend", "docling"),
                        "artifact_version": artifact_version,
                    },
                )
                cache_conn.commit()
            finally:
                cache_conn.close()
        else:
            for span in spans:
                span.parent_discovery_id = record.id

        # Stages D–G: identical to mine_discovery_record_spans
        state = "NC"
        company = self._infer_company_from_record(record, pages=pages)

        from duke_rates.historical.ncuc.family_search_terms import all_profiles
        family_prefix = "nc-progress" if company == "progress" else "nc-carolinas"
        _profile_map = {
            p.leaf: p for p in all_profiles() if p.family_key.startswith(family_prefix)
        }

        supported_families = []
        try:
            conn = self.repository._connect()
            rows = conn.execute(
                "SELECT family_key, title, schedule_code, family_type, current_document_id FROM tariff_families "
                "WHERE family_key LIKE ?",
                (f"{family_prefix}%",),
            ).fetchall()
            for row in rows:
                fk, title, code, ftype, current_document_id = tuple(row)
                if _is_generic_provisional_family_key(fk):
                    continue
                if _is_low_specificity_historical_family(
                    current_document_id=current_document_id,
                    title=title,
                    code=code,
                ):
                    continue
                leaf = fk.split("-")[-1]
                profile = _profile_map.get(leaf)
                aliases = _build_family_matching_aliases(
                    family_key=fk,
                    title=title,
                    code=code,
                    profile_aliases=list(profile.aliases) if profile else [],
                )
                category = _family_type_to_category(ftype)
                supported_families.append({
                    "family_id": fk,
                    "aliases": aliases,
                    "leaf_no": leaf,
                    "code": code,
                    "target_ref": type("T", (), {
                        "family_key": fk,
                        "title": title or "",
                        "leaf_no": leaf,
                        "category": category,
                    })(),
                })
        except Exception as exc:
            logger.warning("Failed to build span-matching families (docling seam): %s", exc)
            return []

        created_ids = []
        for span in spans:
            self._apply_record_matching_hints(span, record)
            legacy_hint = self._apply_legacy_attachment_matching_hints(span, record)
            classification = classify_span_against_families(span, supported_families)
            best_family_key = classification.label if classification else None
            if legacy_hint:
                hinted_family_key = str(legacy_hint.get("family_key") or "")
                if hinted_family_key and any(
                    fam["family_id"] == hinted_family_key for fam in supported_families
                ) and (
                    not best_family_key
                    or best_family_key != hinted_family_key
                    or _is_generic_provisional_family_key(best_family_key)
                ):
                    best_family_key = hinted_family_key
            target = next((f["target_ref"] for f in supported_families if f["family_id"] == best_family_key), None)
            if best_family_key and target and self._should_skip_weak_family_match(span, best_family_key):
                continue
            if not best_family_key or not target:
                if self._should_skip_low_signal_unmatched_span(span):
                    continue
                provisional_target = self._ensure_provisional_family_for_span(
                    state=state,
                    company=company,
                    record=record,
                    span=span,
                )
                if provisional_target is None:
                    continue
                best_family_key = provisional_target.family_key
                target = provisional_target

            extract_dates_from_span(span, full_text_pages)
            effective_start = None
            if hasattr(span, "dates") and span.dates:
                effective_start = span.dates[0].date_value

            from dateutil.parser import parse as parse_date
            try:
                snapshot_dt = parse_date(record.filing_date).isoformat() if record.filing_date else "1970-01-01T00:00:00Z"
            except Exception:
                snapshot_dt = "1970-01-01T00:00:00Z"

            hist_doc = HistoricalDocumentRecord(
                family_key=best_family_key,
                title=f"{target.title} (Span {span.start_page}-{span.end_page})",
                state=state,
                company=company,
                category=target.category,
                kind="pdf",
                canonical_url=str(record.discovered_url or ""),
                archived_url=f"docling://{local_path}#page={span.start_page}" if local_path else "",
                snapshot_timestamp=snapshot_dt,
                local_path=local_path,
                content_hash=str(record.content_hash or ""),
                content_type="application/pdf",
                direct_status_code=200,
                direct_downloadable=True,
                leaf_no=target.leaf_no,
                effective_start=effective_start,
                start_page=span.start_page,
                end_page=span.end_page,
                evidence_json=json.dumps(span.evidence_score_breakdown.get(best_family_key, {})),
                retrieved_at=record.fetched_at or datetime.now(UTC).isoformat(),
            )
            doc_id = self.repository.upsert_historical_document(hist_doc)
            if doc_id and doc_id not in created_ids:
                created_ids.append(doc_id)

            # Record the family-mapping decision (same logic as the
            # mine_discovery_record_spans path — see that function for the
            # rationale behind the override-recording.)
            if classification and doc_id:
                cls_conn = self.repository._connect()
                try:
                    cls_to_record = classification
                    if best_family_key != classification.label:
                        cls_to_record = classification.model_copy(
                            update={
                                "label": best_family_key,
                                "metadata": {
                                    **classification.metadata,
                                    "classifier_label": classification.label,
                                    "classifier_confidence": classification.confidence,
                                    "override_source": "legacy_attachment_hint",
                                },
                            }
                        )
                    record_classification(
                        cls_conn,
                        subject_kind="historical_document",
                        subject_id=str(doc_id),
                        stage="family_mapping",
                        result=cls_to_record,
                    )
                    cls_conn.commit()
                except Exception as exc:
                    logger.debug("Failed to record family_mapping classification (docling seam): %s", exc)
                finally:
                    cls_conn.close()

        if spans and local_path:
            cache_conn = self.repository._connect()
            try:
                save_span_artifacts(
                    cache_conn,
                    discovery_record_id=record.id,
                    source_pdf=str(local_path),
                    file_hash=file_hash or None,
                    spans=spans,
                    metadata={
                        "source_backend": (page_metadata or {}).get("source_backend", "docling"),
                        "artifact_version": artifact_version,
                        "dated_span_cache": True,
                    },
                )
                cache_conn.commit()
            finally:
                cache_conn.close()

        return created_ids

    @staticmethod
    def _apply_record_matching_hints(span, record: NcucDiscoveryRecord) -> None:
        """
        Seed span matching with filing-level hints.

        Some NCUC filings have a cover letter plus a short tariff attachment, or
        a dynamic-PDF text layer that preserves the filing title more cleanly
        than the leaf heading itself. Feed those record-level clues into the
        span before family matching so short rider codes like EE/SG/BPM can
        still bind to known families.
        """
        text_candidates: list[str] = []
        for candidate in (
            record.filing_title,
            record.page_title,
        ):
            if candidate:
                text_candidates.append(candidate)

        if record.metadata_json:
            try:
                metadata = json.loads(record.metadata_json)
            except Exception:
                metadata = {}
            pdf_mining = metadata.get("pdf_content_mining") or {}
            for key in ("selected_title", "derived_title"):
                value = pdf_mining.get(key)
                if value:
                    text_candidates.append(str(value))

        for text in text_candidates:
            if text not in span.header_footer_snippets:
                span.header_footer_snippets.append(text)

        record_leafs = set(record.referenced_leaf_nos or [])
        for text in text_candidates:
            record_leafs.update(extract_leaf_nos(text))
            span.extracted_schedule_titles.update(extract_schedule_codes(text))
            span.extracted_schedule_titles.update(extract_rider_codes(text))
        span.extracted_leaf_nos.update(record_leafs)

    def _load_legacy_attachment_hints(
        self,
        record: NcucDiscoveryRecord,
    ) -> list[dict[str, object]]:
        """Load legacy raw-attachment hints associated with one regulator PDF."""
        def _normalize_path(value: str | None) -> str:
            return str(Path(str(value or "")).as_posix()).lower()

        def _extract_nested_metadata(metadata: dict[str, object]) -> dict[str, object]:
            current: object = metadata
            for _ in range(3):
                if not isinstance(current, dict):
                    return {}
                nested = current.get("metadata_json")
                if not nested:
                    return current
                if isinstance(nested, dict):
                    current = nested
                    continue
                if not isinstance(nested, str):
                    return current
                try:
                    current = json.loads(nested)
                except Exception:
                    return current
            return current if isinstance(current, dict) else {}

        target_local_path = str(record.local_path or "")
        if not target_local_path:
            return []
        normalized_target_local_path = _normalize_path(target_local_path)

        with self.repository._connect() as conn:
            rows = conn.execute(
                """
                SELECT family_key, title, metadata_json
                FROM historical_documents
                WHERE start_page IS NULL
                """
            ).fetchall()

        matched_hints: list[dict[str, object]] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except Exception:
                metadata = {}
            nested = _extract_nested_metadata(metadata)
            if _normalize_path(str(nested.get("local_file") or "")) != normalized_target_local_path:
                continue
            matched_hints.append(
                {
                    "family_key": row["family_key"],
                    "title": row["title"],
                    "parse_text_metadata": nested.get("parse_text_metadata") or {},
                    "family_key_override": nested.get("family_key_override"),
                }
            )

        hints: list[dict[str, object]] = []
        for hint in matched_hints:
            family_key = str(hint.get("family_key") or "")
            if not family_key:
                continue
            title_candidates = _dedupe_strings([str(hint.get("title") or "")])
            matched_terms: list[str] = []
            parse_text_metadata = hint.get("parse_text_metadata") or {}
            if isinstance(parse_text_metadata, dict):
                matched_terms.extend(str(term) for term in parse_text_metadata.get("matched_terms") or [])
            family_code = (
                str(parse_text_metadata.get("family_code") or "")
                if isinstance(parse_text_metadata, dict)
                else ""
            )
            family_override = str(hint.get("family_key_override") or "")
            leaf_match = re.search(r"leaf-(\d{1,4})$", family_key.lower())
            override_leaf_match = re.search(r"leaf-(\d{1,4})$", family_override.lower())
            hints.append(
                {
                    "family_key": family_key,
                    "title_candidates": title_candidates,
                    "matched_terms": _dedupe_strings(matched_terms),
                    "leaf_no": (
                        leaf_match.group(1)
                        if leaf_match
                        else override_leaf_match.group(1)
                        if override_leaf_match
                        else family_code
                        if family_code.isdigit()
                        else None
                    ),
                    "family_key_override": family_override or None,
                }
            )
        return hints

    @staticmethod
    def _select_legacy_attachment_hint(
        span,
        legacy_hints: list[dict[str, object]],
    ) -> dict[str, object] | None:
        if not legacy_hints:
            return None

        span_text_parts = list(span.header_footer_snippets) + list(span.extracted_schedule_titles)
        normalized_span_text = " ".join(_normalize_identifier_token(part) for part in span_text_parts if part)
        span_leafs = {str(leaf) for leaf in span.extracted_leaf_nos if str(leaf)}
        span_codes = {
            code.upper()
            for title in span.extracted_schedule_titles
            for code in (extract_schedule_codes(title) + extract_rider_codes(title))
            if code
        }

        scored: list[tuple[int, dict[str, object]]] = []
        for hint in legacy_hints:
            score = 0
            matched_terms = [str(term) for term in hint.get("matched_terms", []) if term]
            hint_leaf = str(hint.get("leaf_no") or "")
            if hint_leaf and hint_leaf in span_leafs:
                score += 30 if len(span_leafs) <= 2 else 10
            for term in matched_terms:
                normalized_term = _normalize_identifier_token(term)
                if not normalized_term:
                    continue
                if normalized_term in normalized_span_text:
                    score += 20
                if term.upper() in span_codes:
                    score += 15
            for title in hint.get("title_candidates", []):
                normalized_title = _normalize_identifier_token(str(title))
                if normalized_title and normalized_title in normalized_span_text:
                    score += 10
            if score:
                scored.append((score, hint))

        if not scored:
            return None

        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best_hint = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0
        if best_score < 20:
            return None
        if second_score and best_score - second_score < 10:
            return None
        return best_hint

    def _apply_legacy_attachment_matching_hints(
        self,
        span,
        record: NcucDiscoveryRecord,
    ) -> dict[str, object] | None:
        legacy_hints = self._load_legacy_attachment_hints(record)
        legacy_hint = self._select_legacy_attachment_hint(span, legacy_hints)
        if not legacy_hint:
            return None

        for title in legacy_hint.get("title_candidates", []):
            if title not in span.header_footer_snippets:
                span.header_footer_snippets.append(title)
            span.extracted_schedule_titles.add(title)
            span.extracted_schedule_titles.update(extract_schedule_codes(title))
            span.extracted_schedule_titles.update(extract_rider_codes(title))
            span.extracted_leaf_nos.update(extract_leaf_nos(title))

        for term in legacy_hint.get("matched_terms", []):
            span.extracted_schedule_titles.add(term)
            span.extracted_schedule_titles.update(extract_schedule_codes(term))
            span.extracted_schedule_titles.update(extract_rider_codes(term))
            span.extracted_leaf_nos.update(extract_leaf_nos(term))

        leaf_no = legacy_hint.get("leaf_no")
        if isinstance(leaf_no, str) and leaf_no:
            span.extracted_leaf_nos.add(leaf_no)
        return legacy_hint

    def _ensure_provisional_family_for_span(
        self,
        *,
        state: str,
        company: str,
        record: NcucDiscoveryRecord,
        span,
    ):
        """
        Create a historical-only tariff family for strong unmatched spans.

        This is intentionally conservative and only triggers when we have a
        tariff-classified span plus a usable descriptive title, which preserves
        historically relevant documents like pilot programs without broadening
        family creation to noisy procedural records.
        """
        if span.doc_type != "tariff":
            return None

        title_candidates = list(span.extracted_schedule_titles)
        title_candidates.extend(span.header_footer_snippets)
        title_candidates.extend(
            value
            for value in (record.filing_title, record.page_title)
            if value
        )
        ranked_candidates = sorted(
            (
                candidate.strip()
                for candidate in title_candidates
                if _looks_like_provisional_title(candidate)
            ),
            key=_score_provisional_title_candidate,
        )
        title = next(iter(ranked_candidates), None)
        if not title:
            return None

        family_type = "program" if "PROGRAM" in title.upper() else "rider" if "RIDER" in title.upper() else "rate_schedule"
        token = _normalize_identifier_token(title)
        if not token:
            return None

        if family_type == "program":
            tariff_identifier = f"program-{token}"
        elif family_type == "rider":
            tariff_identifier = f"rider-{token}"
        else:
            tariff_identifier = f"doc-{token}"
        family_key = f"{state.lower()}-{company.lower()}-{tariff_identifier}"
        existing = self.repository.get_tariff_family(family_key)
        if existing is None:
            from duke_rates.models.tariff import TariffFamilyRecord

            aliases = _dedupe_strings(
                [title, *(candidate for candidate in title_candidates if _looks_like_provisional_title(candidate))]
            )
            self.repository.upsert_tariff_family(
                TariffFamilyRecord(
                    family_key=family_key,
                    state=state.upper(),
                    company=company,
                    tariff_identifier=tariff_identifier,
                    schedule_code=token,
                    family_type=family_type,
                    title=title,
                    aliases=aliases,
                    notes="Provisional historical family created from unmatched NCUC tariff span.",
                )
            )

        leaf_no = next(iter(sorted(span.extracted_leaf_nos)), None)
        return type(
            "T",
            (),
            {
                "family_key": family_key,
                "title": title,
                "leaf_no": leaf_no,
                "category": _family_type_to_category(family_type),
            },
        )()

    @staticmethod
    def _infer_company_from_record(
        record: NcucDiscoveryRecord,
        *,
        pages: list[PageEvidence] | None = None,
    ) -> str:
        """
        Determine whether a filing belongs to Progress or Carolinas.

        Older discovery records may have `utility="Duke Energy Progress"` as a
        default even when the actual filing title/path identify Duke Power or
        Duke Energy Carolinas. Prefer explicit filing metadata over the utility
        field to avoid cross-company family matching.
        """
        text_parts = [
            record.filing_title or "",
            record.page_title or "",
            record.local_path or "",
            record.discovered_url or "",
            record.viewer_url or "",
            record.download_url or "",
            record.docket_number or "",  # E-2/E-7 handled by secondary context clues
        ]
        if pages:
            for page in pages[:5]:
                text_parts.append(page.text_content or "")
                text_parts.extend(page.header_candidates[:5])
                text_parts.extend(page.footer_candidates[:5])
        filing_text = " ".join(text_parts).lower()
        fallback = "progress"
        utility_text = record.utility or ""
        if utility_text and "carolina" in utility_text.lower() and "progress" not in utility_text.lower():
            fallback = "carolinas"

        utility_company = normalize_duke_company(utility_text, fallback=fallback, state="NC")
        filing_aliases = detect_duke_company(filing_text)
        # Mixed-company filing titles occur in shared filings; when the record's
        # utility is already explicit, keep that company rather than letting a
        # broad mixed title or path token flip the assignment.
        if len(filing_aliases) > 1 and utility_company in filing_aliases:
            return utility_company

        company = normalize_duke_company(filing_text, fallback=None, state="NC")
        if company:
            return company
        return utility_company or fallback

    @staticmethod
    def _should_skip_weak_family_match(span, family_key: str) -> bool:
        """
        Reject broad low-evidence matches that only look tariff-like because of
        generic vocabulary plus a weak title-token overlap.

        This protects long procedural reports from being imported as tariff
        sheets when they mention topics such as hourly pricing but do not carry
        leaf numbers or strong schedule/rider markers.
        """
        page_span = max(1, int(span.end_page) - int(span.start_page) + 1)
        if page_span < 8:
            return False

        breakdown = span.evidence_score_breakdown.get(family_key) or {}
        if not breakdown:
            return False

        if any(
            key in breakdown
            for key in ("explicit_leaf_hit", "schedule_code_hit", "summary_sheet_bonus")
        ):
            return False

        if span.extracted_leaf_nos:
            return False

        extracted_codes: set[str] = set()
        for title in span.extracted_schedule_titles:
            extracted_codes.update(extract_schedule_codes(title))
            extracted_codes.update(extract_rider_codes(title))
        if extracted_codes:
            return False

        weak_keys = {"heading_alias_similarity", "tariff_vocab_density"}
        return set(breakdown).issubset(weak_keys)

    @staticmethod
    def _should_skip_low_signal_unmatched_span(span) -> bool:
        """
        Do not create provisional families for broad low-signal spans that lack
        leaf numbers and schedule/rider markers.
        """
        page_span = max(1, int(span.end_page) - int(span.start_page) + 1)
        if page_span < 8:
            return False
        if span.extracted_leaf_nos:
            return False

        extracted_codes: set[str] = set()
        for title in span.extracted_schedule_titles:
            extracted_codes.update(extract_schedule_codes(title))
            extracted_codes.update(extract_rider_codes(title))
        return not extracted_codes

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    # Static fallback: leaf/schedule code -> family key (all 111 NC Progress leaves)
    # Built from family_search_terms profiles so it stays in sync with the full profile set.
    @staticmethod
    def _build_static_code_to_family() -> dict[str, str]:
        from duke_rates.historical.ncuc.family_search_terms import all_profiles
        result: dict[str, str] = {}
        for p in all_profiles():
            if not p.family_key.startswith("nc-progress"):
                continue
            fk = f"ncuc-dep-{p.leaf}"
            result[p.leaf] = fk
            result[p.leaf.lstrip("0")] = fk
            if p.schedule_code:
                result[p.schedule_code] = fk
        return result

    @property
    def _STATIC_CODE_TO_FAMILY(self) -> dict[str, str]:
        if not hasattr(self, "_static_map_cache"):
            self._static_map_cache = self._build_static_code_to_family()
        return self._static_map_cache

    def _resolve_family_keys(self, record: NcucDiscoveryRecord) -> list[str]:
        """
        Try to resolve the record to known family keys based on schedule
        and rider codes.  First tries the DB-backed family targets; if that
        yields nothing (bootstrap state with no documents yet), falls back to
        a static schedule-code → family-key mapping.
        """
        matched: list[str] = []

        # --- DB-backed resolution (preferred when documents are loaded) ---
        try:
            targets = build_progress_nc_family_targets(self.repository)
        except Exception:
            targets = {}

        if targets:
            for code in record.referenced_schedule_codes:
                for fk, target in targets.items():
                    if target.code and target.code.lstrip("0") == code.lstrip("0"):
                        if fk not in matched:
                            matched.append(fk)
                    if target.leaf_no and target.leaf_no.lstrip("0") == code.lstrip("0"):
                        if fk not in matched:
                            matched.append(fk)

            for leaf in record.referenced_leaf_nos:
                for fk, target in targets.items():
                    if target.leaf_no and target.leaf_no.lstrip("0") == leaf.lstrip("0"):
                        if fk not in matched:
                            matched.append(fk)

            if matched:
                return matched

        # --- Static fallback (bootstrap / no documents in DB yet) ---
        for code in record.referenced_schedule_codes + record.referenced_leaf_nos:
            fk = self._STATIC_CODE_TO_FAMILY.get(code.lstrip("0"))
            if fk and fk not in matched:
                matched.append(fk)

        return matched

    def _upsert_historical_lead(
        self,
        record: NcucDiscoveryRecord,
        *,
        family_key: str,
    ) -> int:
        # Find target metadata for display
        title = record.filing_title or record.docket_number or "NCUC document"
        # Build display-friendly codes
        schedule_code = record.referenced_schedule_codes[0] if record.referenced_schedule_codes else None
        rider_code = record.referenced_rider_codes[0] if record.referenced_rider_codes else None
        leaf_ref = record.referenced_leaf_nos[0] if record.referenced_leaf_nos else None

        # Determine the target's family metadata from DB
        leads_existing = self.repository.list_historical_leads(family_key=family_key)
        if leads_existing:
            target_title = leads_existing[0].target_title
            target_leaf_no = leads_existing[0].target_leaf_no
            target_code = leads_existing[0].target_code
            family_type = leads_existing[0].family_type
        else:
            target_title = title
            target_leaf_no = leaf_ref
            target_code = schedule_code
            family_type = "schedule" if schedule_code else "rider"

        relevance = score_relevance(
            title=title,
            docket=record.docket_number,
            schedule_codes=record.referenced_schedule_codes,
            rider_codes=record.referenced_rider_codes,
        )

        lead = HistoricalLeadRecord(
            family_key=family_key,
            target_leaf_no=target_leaf_no,
            target_code=target_code,
            target_title=target_title,
            family_type=family_type,
            category="tariff",
            source_class=NCUC_SOURCE_CLASS,
            provenance_class=NCUC_PROVENANCE_CLASS,
            source_label="ncuc-edocket",
            source_location=record.discovered_url,
            source_url=record.discovered_url,
            extracted_url=record.download_url or record.viewer_url or record.discovered_url,
            extracted_title=record.filing_title,
            attachment_url=record.attachment_url,
            viewer_url=record.viewer_url,
            hostname="edocket.ncuc.net" if record.discovered_url and "edocket.ncuc.net" in (record.discovered_url or "") else None,
            docket_number=record.docket_number,
            schedule_code=schedule_code,
            rider_code=rider_code,
            leaf_reference=leaf_ref,
            effective_start=record.filing_date,
            extraction_method="ncuc_discovery",
            confidence_score=relevance,
            disposition="new",
            score_notes=[
                f"ncuc_classification={record.filing_classification.value}",
                f"fetch_status={record.fetch_status.value}",
            ],
            notes=[
                f"ncuc_record_id={record.id}",
                f"acquisition_method={record.acquisition_method.value}",
            ] + record.provenance_notes,
            metadata_json=json.dumps(
                {
                    "ncuc_record_id": record.id,
                    "docket_number": record.docket_number,
                    "sub_number": record.sub_number,
                    "filing_classification": record.filing_classification.value,
                    "fetch_status": record.fetch_status.value,
                    "local_path": record.local_path,
                    "content_hash": record.content_hash,
                    "file_size_bytes": record.file_size_bytes,
                },
                sort_keys=True,
            ),
        )
        return self.repository.upsert_historical_lead(lead)

    def _upsert_docket_lead(
        self,
        record: NcucDiscoveryRecord,
        *,
        family_key: str,
    ) -> int:
        referenced_codes = (
            record.referenced_schedule_codes + record.referenced_rider_codes
        )

        docket_lead = RegulatoryDocketLeadRecord(
            family_key=family_key,
            docket_number=record.docket_number or "unknown",
            utility=record.utility,
            proceeding_type=record.proceeding_type or record.filing_classification.value,
            date_start=record.filing_date,
            referenced_codes=referenced_codes,
            evidence_source=record.filing_title or record.discovered_url or "ncuc-discovery",
            evidence_source_type=NCUC_SOURCE_CLASS,
            evidence_source_location=record.discovered_url,
            title=record.filing_title,
            contains_tariff_text=record.filing_classification.value in (
                "tariff_sheets", "compliance_filing", "exhibit"
            ),
            clue_only=(record.fetch_status != NcucFetchStatus.SUCCESS),
            notes=[
                f"ncuc_record_id={record.id}",
                f"acquisition_method={record.acquisition_method.value}",
                f"classification={record.filing_classification.value}",
            ] + record.provenance_notes,
            metadata_json=json.dumps(
                {
                    "ncuc_record_id": record.id,
                    "sub_number": record.sub_number,
                    "exhibit_label": record.exhibit_label,
                    "local_path": record.local_path,
                },
                sort_keys=True,
            ),
        )

        # Score using existing scorer
        try:
            score, notes = score_docket_lead(docket_lead)
            docket_lead.confidence_score = score
            docket_lead.notes.extend(notes)
        except Exception:
            pass

        return self.repository.upsert_regulatory_docket_lead(docket_lead)
