"""
LLM document structure analyst (Phase 6C of parsing-architecture refactor).

Uses a local LLM to review deterministic section boundaries from Phase 6B and
suggest refinements. Agreement boosts confidence; disagreement flags sections
for manual review.

Version 2: Per-section analysis — sends only the pages of one section at a time
(typically 2-5 pages), keeping LLM prompts small and fast.

Plan reference: ``docs/PARSING_ARCHITECTURE_REFACTOR_PLAN.md`` §6.5.

Usage::

    from duke_rates.document_intelligence.document_structure_analyst import (
        DocumentStructureAnalyst,
    )
    from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator

    orch = OllamaOrchestrator(db_path=db_path)
    analyst = DocumentStructureAnalyst(orch, db_path)
    results = analyst.analyze_batch(limit=5)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from .document_sections import (
    SectionBundle,
    SectionType,
    RATE_SECTION_TYPES,
    ensure_schema,
    fetch_sections,
    upsert_section,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM structured output schemas
# ---------------------------------------------------------------------------


class SectionBoundary(BaseModel):
    start_page: int = Field(description="First page of this section (1-indexed)")
    end_page: int = Field(description="Last page of this section (1-indexed)")
    section_type: str = Field(
        default="unknown",
        description="One of: rate_schedule, rider, terms_conditions, cover_letter, "
        "table_of_contents, procedural, unknown",
    )
    schedule_codes: list[str] = Field(default_factory=list)
    rider_codes: list[str] = Field(default_factory=list)
    leaf_numbers: list[str] = Field(default_factory=list)
    title: str = Field(default="")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = Field(default="")

    @model_validator(mode="before")
    @classmethod
    def normalize_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for alias, canonical in [
            ("sectionType", "section_type"),
            ("scheduleCodes", "schedule_codes"),
            ("riderCodes", "rider_codes"),
            ("leafNumbers", "leaf_numbers"),
            ("startPage", "start_page"),
            ("endPage", "end_page"),
        ]:
            if alias in data and canonical not in data:
                data[canonical] = data.pop(alias)
        return data


# Legacy whole-document output — kept for the old analyze_structure() path
class DocumentStructureOutput(BaseModel):
    source_pdf: str = Field(default="")
    sections: list[SectionBoundary] = Field(default_factory=list)
    overall_quality: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: str = Field(default="")

    @model_validator(mode="before")
    @classmethod
    def normalize_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for alias, canonical in [
            ("sourcePdf", "source_pdf"),
            ("overallQuality", "overall_quality"),
        ]:
            if alias in data and canonical not in data:
                data[canonical] = data.pop(alias)
        return data


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

# Per-section prompt — only sends the pages for ONE section (~200-800 chars)
_SECTION_ANALYSIS_PROMPT = """\
You are a tariff document structure analyst for the NCUC.
Review this document section and confirm or correct its type and boundaries.

## Document context
- Source PDF: {source_pdf}
- Document signals: {doc_signals}
- Section index in document: {section_index}

## Section pages
Each row shows one page in this section with OCR text and metadata signals.
{page_summary}

## Current classification
- Determined type: {current_type}
- Start page: {start_page}
- End page: {end_page}
- Confidence: {current_confidence}

## Instructions
1. Confirm or correct the section_type (one of: rate_schedule, rider,
   terms_conditions, cover_letter, table_of_contents, procedural, unknown)
2. Confirm or adjust start_page and end_page if the boundaries look wrong
3. Extract any schedule_codes or rider_codes visible in the page text
4. Extract leaf_numbers (e.g. "331", "532") where present
5. Provide a brief rationale
6. Rate your confidence (0.0-1.0)

Respond with a single JSON object:
{{
  "section_type": "<type>",
  "start_page": <int>,
  "end_page": <int>,
  "schedule_codes": ["<code>"],
  "rider_codes": ["<code>"],
  "leaf_numbers": ["<number>"],
  "title": "",
  "confidence": <float>,
  "rationale": "<one sentence>"
}}

No other text. No markdown fences. JSON only.
"""

# Maximum characters of page text per page in the summary
_MAX_PAGE_TEXT_CHARS: int = 100

# Maximum pages per section sent to the LLM (single-section prompt)
_MAX_SECTION_PAGES: int = 8

# Hard cap on per-section prompt size
_MAX_SECTION_PROMPT_CHARS: int = 3000

# Maximum pages sent in a single boundary-classification prompt
_MAX_BOUNDARY_PAGES: int = 30

# Characters of text per page for boundary classification
_MAX_BOUNDARY_PAGE_CHARS: int = 400

# Per-page boundary classification prompt
_PAGE_BOUNDARY_PROMPT = """\
You are a tariff document structure analyst. For each page below, classify
whether it is the START of a new rate schedule, rider, or tariff sheet
("start") or a CONTINUATION of an existing section ("continuation").

A "start" page typically has:
- A company header (Duke Energy Carolinas/Progress, LLC)
- "Electricity No." or "Leaf No." or a schedule/rider code (e.g. SCHEDULE RS)
- "Superseding" language or a new effective date
- A new service title like "RESIDENTIAL SERVICE" or "SMALL GENERAL SERVICE"

A "continuation" page typically:
- Continues the same schedule's terms or rate table
- Has the same schedule code as preceding pages
- Has no new "Superseding" language

Pages:
{page_summaries}

Respond with a single JSON object:
{{"boundary_pages": [<page_numbers_that_start_new_sections>]}}

No other text. No markdown fences. JSON only.
"""


class PageBoundaryResult(BaseModel):
    boundary_pages: list[int] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Analyst class
# ---------------------------------------------------------------------------


class DocumentStructureAnalyst:
    """Use a local LLM to refine deterministic section boundaries.

    V2: Analyzes one section at a time (2-5 pages typically), so each LLM
    call is fast (<30s) even for large documents.
    """

    def __init__(
        self,
        orchestrator: Any,  # OllamaOrchestrator
        db_path: Path | str,
        *,
        role: str = "document_structure_analyst",
        max_pages: int = 40,
    ) -> None:
        self._orch = orchestrator
        self._db_path = Path(db_path)
        self._role = role
        self._max_pages = max_pages
        ensure_schema(self._db_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select_candidates(
        self, limit: int | None = None,
    ) -> list[tuple[str, int]]:
        """Return (source_pdf, section_index) pairs for sections needing review.

        Candidates are sections with:
        - Confidence < 0.55 (below agreement threshold), AND
        - Page count between 2 and _MAX_SECTION_PAGES (inclusive)
        - Section type is rate-relevant (rate_schedule, rider, terms_conditions)
          or unknown

        Gate-reclassified sections (leaf_header_gate + reclassify) are
        prioritized first — these are the most likely false negatives.
        """
        conn = sqlite3.connect(str(self._db_path))
        try:
            sql = """
            SELECT ds.source_pdf, ds.section_index
            FROM document_sections ds
            JOIN document_identity di ON di.source_pdf = ds.source_pdf
            WHERE ds.overall_confidence < 0.55
              AND (ds.end_page - ds.start_page + 1) BETWEEN 2 AND ?
              AND ds.section_type IN ('rate_schedule', 'rider',
                                      'terms_conditions', 'unknown')
              AND di.inferred_doc_type IN ('compliance_tariff_bundle',
                                           'single_rate_schedule')
            ORDER BY
              CASE WHEN ds.evidence_log_json LIKE '%leaf_header_gate%'
                    AND ds.evidence_log_json LIKE '%reclassify%'
                   THEN 0 ELSE 1 END,
              ds.overall_confidence ASC
            """
            params = [int(_MAX_SECTION_PAGES)]
            if limit:
                sql += " LIMIT ?"
                params.append(int(limit))

            rows = conn.execute(sql, params).fetchall()
            return [(r[0], r[1]) for r in rows]
        finally:
            conn.close()

    def _count_large_section_docs(
        self, limit: int | None = None,
    ) -> int:
        """Count documents with at least one section > _MAX_SECTION_PAGES."""
        conn = sqlite3.connect(str(self._db_path))
        try:
            sql = """
            SELECT COUNT(DISTINCT ds.source_pdf) FROM document_sections ds
            JOIN document_identity di ON di.source_pdf = ds.source_pdf
            WHERE (ds.end_page - ds.start_page + 1) > ?
              AND ds.section_type IN ('rate_schedule', 'rider',
                                      'terms_conditions', 'unknown')
              AND di.inferred_doc_type IN ('compliance_tariff_bundle',
                                           'single_rate_schedule')
            """
            row = conn.execute(sql, [int(_MAX_SECTION_PAGES)]).fetchone()
            count = row[0] if row else 0
            # Apply limit at Python level (limits how many we'd process)
            if limit:
                count = min(count, limit)
            return count
        finally:
            conn.close()

    def analyze_section_boundary(
        self,
        source_pdf: str,
        section_index: int,
    ) -> SectionBoundary | None:
        """Analyze a single section with the LLM.

        Sends only the pages for this one section (2-5 typically).
        Returns None on failure.
        """
        context = self._build_section_context(source_pdf, section_index)
        if context is None:
            return None

        # Truncate page_summary (the variable content) before formatting so
        # the instruction template is always fully preserved in the prompt.
        page_summary = context["page_summary"]
        template_overhead = len(_SECTION_ANALYSIS_PROMPT) + 500
        max_summary_chars = max(500, _MAX_SECTION_PROMPT_CHARS - template_overhead)
        if len(page_summary) > max_summary_chars:
            page_summary = page_summary[:max_summary_chars] + "\n...[truncated]"

        prompt = _SECTION_ANALYSIS_PROMPT.format(
            source_pdf=source_pdf,
            doc_signals=context["doc_signals"],
            section_index=section_index,
            page_summary=page_summary,
            current_type=context["current_type"],
            start_page=context["start_page"],
            end_page=context["end_page"],
            current_confidence=context["current_confidence"],
        )

        run_result = self._orch.generate_json(
            role=self._role,
            prompt=prompt,
            schema=SectionBoundary,
            subject_kind="document_section",
            subject_id=f"{source_pdf}#{section_index}",
            stage="section_analysis",
        )

        if run_result.status not in ("ok", "fallback_used"):
            logger.debug(
                "Section analysis failed for %s#%d: status=%s model=%s",
                source_pdf, section_index,
                run_result.status, run_result.model,
            )
            return None

        result: SectionBoundary = run_result.result
        logger.debug(
            "Section analysis for %s#%d: type=%s conf=%.2f model=%s",
            source_pdf, section_index,
            result.section_type, result.confidence, run_result.model,
        )
        return result

    def merge_section_result(
        self,
        source_pdf: str,
        section_index: int,
        llm_output: SectionBoundary,
    ) -> bool:
        """Merge a single-section LLM result into the database.

        Returns True if the section was updated, False if not found.
        """
        existing = fetch_sections(self._db_path, source_pdf)
        target = None
        for es in existing:
            if es.section_index == section_index:
                target = es
                break

        if target is None:
            return False

        det_type = (
            target.section_type.value
            if hasattr(target.section_type, 'value')
            else str(target.section_type)
        )
        llm_type = llm_output.section_type
        agrees = llm_type == det_type

        evidence = {
            "source": "llm_section_analyst",
            "model_proposed_type": llm_type,
            "llm_confidence": llm_output.confidence,
            "agrees_with_deterministic": agrees,
            "llm_rationale": (
                llm_output.rationale[:500] if llm_output.rationale else ""
            ),
        }

        if agrees:
            evidence["action"] = "confidence_boost"
            target.overall_confidence = min(
                1.0, round(target.overall_confidence + 0.15, 3)
            )
        else:
            evidence["action"] = "needs_review"
            # Apply the LLM's proposed type, mapping non-standard names
            mapped_type = self._map_llm_type(llm_type)
            evidence["model_proposed_type_raw"] = llm_type
            evidence["model_proposed_type"] = (
                mapped_type.value if hasattr(mapped_type, 'value')
                else str(mapped_type)
            )
            if mapped_type != SectionType.UNKNOWN:
                target.section_type = mapped_type

        if llm_output.schedule_codes:
            existing_codes = set(target.schedule_codes)
            for c in llm_output.schedule_codes:
                if c not in existing_codes:
                    target.schedule_codes.append(c)
        if llm_output.rider_codes:
            existing_riders = set(target.rider_codes)
            for c in llm_output.rider_codes:
                if c not in existing_riders:
                    target.rider_codes.append(c)
        if llm_output.leaf_numbers:
            existing_leaves = set(target.leaf_numbers)
            for n in llm_output.leaf_numbers:
                if n not in existing_leaves:
                    target.leaf_numbers.append(n)

        target.evidence_log.append(evidence)
        upsert_section(self._db_path, target)
        return True

    def analyze_batch(
        self,
        *,
        limit: int | None = None,
        dry_run: bool = False,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Run per-section analysis on candidate sections.

        Phase 1: Find and split large (>8 page) sections using the
                 per-page boundary classifier.
        Phase 2: Analyze individual small sections with the LLM.

        When *deadline* is provided (a ``time.monotonic()`` timestamp),
        the loop stops as soon as it's exceeded — protects against a
        single large doc consuming the entire wall-clock budget.

        Returns summary dict.
        """
        import time as _time

        # Phase 1: Split large sections so we get small per-section units
        large_results = self.analyze_large_sections_batch(
            limit=limit, dry_run=dry_run, deadline=deadline,
        )

        # Phase 2: Analyze individual sections (now including newly-split ones)
        candidates = self.select_candidates(limit=limit)
        results: dict[str, Any] = {
            "candidates": len(candidates),
            "analyzed": 0,
            "merged": 0,
            "failed": 0,
            "skipped": 0,
            "agreed": 0,
            "disagreed": 0,
            "per_section": [],
            "large_sections": large_results,
        }

        for source_pdf, section_index in candidates:
            if dry_run:
                results["skipped"] += 1
                continue
            if deadline is not None and _time.monotonic() >= deadline:
                results["skipped"] += 1
                results.setdefault("stopped_at_deadline", True)
                continue

            try:
                output = self.analyze_section_boundary(source_pdf, section_index)
                if output is None:
                    results["failed"] += 1
                    results["per_section"].append({
                        "source_pdf": source_pdf,
                        "section_index": section_index,
                        "status": "failed",
                    })
                    continue

                results["analyzed"] += 1
                updated = self.merge_section_result(
                    source_pdf, section_index, output,
                )
                if updated:
                    results["merged"] += 1
                    det_type = self._get_section_type(source_pdf, section_index)
                    if output.section_type == det_type:
                        results["agreed"] += 1
                    else:
                        results["disagreed"] += 1

                results["per_section"].append({
                    "source_pdf": source_pdf,
                    "section_index": section_index,
                    "status": "ok",
                    "llm_type": output.section_type,
                    "llm_confidence": output.confidence,
                })
            except Exception:
                logger.warning(
                    "Batch analysis failed for %s#%d",
                    source_pdf, section_index, exc_info=True,
                )
                results["failed"] += 1
                results["per_section"].append({
                    "source_pdf": source_pdf,
                    "section_index": section_index,
                    "status": "error",
                })

        return results

    # ------------------------------------------------------------------
    # Per-page boundary classification
    # ------------------------------------------------------------------

    def classify_page_boundaries(
        self,
        source_pdf: str,
        page_numbers: list[int],
        *,
        dry_run: bool = False,
    ) -> list[int]:
        """Classify candidate pages as section-starts or continuations.

        Sends page texts to an LLM in a single batch call. Returns the
        list of page numbers that the LLM identifies as section starts.

        Args:
            source_pdf: Document path.
            page_numbers: Candidate page numbers to classify (1-indexed).
            dry_run: If True, return empty list without LLM call.

        Returns:
            List of page numbers (1-indexed) that are section starts.
        """
        if not page_numbers or dry_run:
            return []

        # Fetch page texts
        page_texts = self._fetch_page_texts(source_pdf, page_numbers)
        if not page_texts:
            return []

        # Build page summaries
        summaries: list[str] = []
        for pn in page_numbers:
            text = page_texts.get(pn, "")
            if not text:
                continue
            # Take first N chars — the header area is what matters
            excerpt = text[:_MAX_BOUNDARY_PAGE_CHARS]
            # Clean for prompt: collapse whitespace, strip
            excerpt = " ".join(excerpt.split())
            summaries.append(f"Page {pn}: {excerpt}")

        if not summaries:
            return []

        prompt = _PAGE_BOUNDARY_PROMPT.format(
            page_summaries="\n\n".join(summaries),
        )

        run_result = self._orch.generate_json(
            role=self._role,
            prompt=prompt,
            schema=PageBoundaryResult,
            subject_kind="page_boundary",
            subject_id=source_pdf,
            stage="boundary_classification",
        )

        if run_result.status not in ("ok", "fallback_used"):
            logger.debug(
                "Boundary classification failed for %s: status=%s",
                source_pdf, run_result.status,
            )
            return []

        result: PageBoundaryResult = run_result.result
        valid = [p for p in result.boundary_pages if p in page_numbers]
        logger.debug(
            "Boundary classification for %s: %d/%d candidates confirmed",
            source_pdf, len(valid), len(page_numbers),
        )
        return sorted(valid)

    def find_boundaries_in_large_sections(
        self,
        source_pdf: str,
        *,
        dry_run: bool = False,
    ) -> list[int]:
        """Find section boundaries in large (>8 page) sections.

        For sections that are too large for per-section LLM analysis,
        classifies each page as section-start or continuation to
        discover internal boundaries the deterministic aggregator missed.

        Returns a list of page numbers (1-indexed) that should be
        section boundaries, suitable for feeding back to the aggregator.
        """
        sections = fetch_sections(self._db_path, source_pdf)
        if not sections:
            return []

        # Collect pages from sections > 8 pages that are rate-relevant
        candidate_pages: list[int] = []
        for sec in sections:
            n_pages = sec.end_page - sec.start_page + 1
            if n_pages <= _MAX_SECTION_PAGES:
                continue
            if sec.section_type not in RATE_SECTION_TYPES:
                # Skip procedural, cover_letter — not our target
                if sec.section_type not in (
                    SectionType.TERMS_CONDITIONS, SectionType.UNKNOWN,
                ):
                    continue
            # Add all pages EXCEPT the first (which is already a boundary)
            # and the last (which is already a boundary end)
            for pn in range(sec.start_page + 1, sec.end_page):
                candidate_pages.append(pn)

        if not candidate_pages:
            return []

        # For large docs, chunk into _MAX_BOUNDARY_PAGES batches and
        # call the LLM for each chunk independently — sampling would
        # miss boundaries between sampled pages.
        all_boundaries: list[int] = []
        for chunk_start in range(0, len(candidate_pages), _MAX_BOUNDARY_PAGES):
            chunk = candidate_pages[chunk_start:chunk_start + _MAX_BOUNDARY_PAGES]
            if len(chunk) < 3:
                # Too few pages to classify meaningfully — skip
                continue
            chunk_boundaries = self.classify_page_boundaries(
                source_pdf, chunk, dry_run=dry_run,
            )
            all_boundaries.extend(chunk_boundaries)

        return sorted(set(all_boundaries))

    # ------------------------------------------------------------------
    # Large-section splitting (per-page boundary classifier integration)
    # ------------------------------------------------------------------

    def _split_section_at_page(
        self, source_pdf: str, boundary_page: int,
    ) -> int:
        """Split the section containing boundary_page at that page.

        The page before boundary_page ends one section; boundary_page starts
        a new section. Returns the number of sections split (0 or 1).
        """
        sections = fetch_sections(self._db_path, source_pdf)
        target: SectionBundle | None = None
        for sec in sections:
            if sec.start_page < boundary_page <= sec.end_page:
                target = sec
                break

        if target is None:
            return 0

        # Don't split if boundary is the first page (already a section start)
        if boundary_page <= target.start_page:
            return 0

        original_start = target.start_page
        original_end = target.end_page
        # Preserve original confidence — splitting doesn't reduce accuracy,
        # it improves granularity. Both halves keep the same confidence.
        original_confidence = target.overall_confidence

        # Update original section to end before boundary
        target.end_page = boundary_page - 1
        target.evidence_log.append({
            "source": "page_boundary_classifier",
            "action": "split_end",
            "boundary_page": boundary_page,
            "original_range": [original_start, original_end],
            "new_range": [target.start_page, target.end_page],
        })
        upsert_section(self._db_path, target)

        # Create new section starting at boundary
        new_idx = max((s.section_index for s in sections), default=-1) + 1
        new_section = SectionBundle(
            source_pdf=source_pdf,
            section_index=new_idx,
            start_page=boundary_page,
            end_page=original_end,
            section_type=target.section_type,
            schedule_codes=list(target.schedule_codes),
            rider_codes=list(target.rider_codes),
            leaf_numbers=list(target.leaf_numbers),
            detected_titles=list(target.detected_titles),
            overall_confidence=original_confidence,
            evidence_log=[{
                "source": "page_boundary_classifier",
                "action": "split_start",
                "boundary_page": boundary_page,
                "original_range": [original_start, original_end],
                "new_range": [boundary_page, original_end],
                "parent_section_index": target.section_index,
            }],
            parent_section_index=target.section_index,
        )
        upsert_section(self._db_path, new_section)
        logger.debug(
            "Split %s section %d at page %d: [%d-%d] -> [%d-%d] + [%d-%d]",
            source_pdf[-60:], target.section_index, boundary_page,
            original_start, original_end,
            target.start_page, target.end_page,
            new_section.start_page, new_section.end_page,
        )
        return 1

    def _apply_large_section_boundaries(
        self, source_pdf: str, boundary_pages: list[int],
    ) -> int:
        """Split sections at confirmed boundary pages. Returns split count."""
        split_count = 0
        for bp in sorted(boundary_pages):
            split_count += self._split_section_at_page(source_pdf, bp)
        return split_count

    def analyze_large_sections_batch(
        self,
        *,
        limit: int | None = None,
        dry_run: bool = False,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Find and split large (>8 page) sections using the per-page classifier.

        When *deadline* is provided, the loop stops as soon as the
        ``time.monotonic()`` cutoff is exceeded — important because a single
        large doc can issue many LLM chunk calls.

        Returns summary dict.
        """
        import time as _time

        results: dict[str, Any] = {
            "docs_candidates": 0,
            "docs_analyzed": 0,
            "boundaries_found": 0,
            "sections_split": 0,
            "failed": 0,
        }

        # Select docs with at least one section > _MAX_SECTION_PAGES.
        # Order by the smallest large section first — smaller docs cost
        # fewer LLM calls (fewer chunks), so we get higher throughput.
        #
        # IMPORTANT: Filter by document-level classification from
        # document_identity. Only compliance_tariff_bundle and
        # single_rate_schedule docs benefit from LLM boundary analysis.
        # Non-tariff docs (hearing exhibits, DSM evaluations) waste GPU
        # cycles and produce garbage sections.
        conn = sqlite3.connect(str(self._db_path))
        try:
            sql = """
            SELECT ds.source_pdf, MIN(ds.end_page - ds.start_page + 1) as min_pages
            FROM document_sections ds
            JOIN document_identity di ON di.source_pdf = ds.source_pdf
            WHERE (ds.end_page - ds.start_page + 1) > ?
              AND ds.section_type IN ('rate_schedule', 'rider',
                                      'terms_conditions', 'unknown')
              AND di.inferred_doc_type IN ('compliance_tariff_bundle',
                                           'single_rate_schedule')
            GROUP BY ds.source_pdf
            ORDER BY min_pages ASC
            """
            params: list[Any] = [int(_MAX_SECTION_PAGES)]
            if limit:
                sql += " LIMIT ?"
                params.append(int(limit))
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        results["docs_candidates"] = len(rows)

        for (source_pdf, _min_pages) in rows:
            if dry_run:
                continue
            if deadline is not None and _time.monotonic() >= deadline:
                results.setdefault("stopped_at_deadline", True)
                break

            try:
                boundary_pages = self.find_boundaries_in_large_sections(
                    source_pdf, dry_run=dry_run,
                )
                results["docs_analyzed"] += 1
                if boundary_pages:
                    results["boundaries_found"] += len(boundary_pages)
                    split_count = self._apply_large_section_boundaries(
                        source_pdf, boundary_pages,
                    )
                    results["sections_split"] += split_count
            except Exception:
                logger.warning(
                    "Large-section analysis failed for %s",
                    source_pdf, exc_info=True,
                )
                results["failed"] += 1

        return results

    def _fetch_page_texts(
        self, source_pdf: str, page_numbers: list[int],
    ) -> dict[int, str]:
        """Fetch text_content for specific pages of a document."""
        if not page_numbers:
            return {}
        conn = sqlite3.connect(str(self._db_path))
        try:
            placeholders = ",".join("?" for _ in page_numbers)
            rows = conn.execute(
                f"SELECT page_number, text_content FROM ncuc_page_artifacts "
                f"WHERE source_pdf = ? AND page_number IN ({placeholders})",
                [source_pdf] + page_numbers,
            ).fetchall()
            return {r[0]: (r[1] or "") for r in rows}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Internal — section context building
    # ------------------------------------------------------------------

    @staticmethod
    def _map_llm_type(llm_type: str) -> SectionType:
        """Map non-standard LLM type names to valid SectionType values.

        LLMs sometimes return "Appendix", "Exhibit H", etc. — these are
        document sections, not valid section types. Map to the closest
        valid type or SectionType.UNKNOWN.
        """
        _TYPE_MAP: dict[str, SectionType] = {
            "appendix": SectionType.TERMS_CONDITIONS,
            "exhibit": SectionType.TERMS_CONDITIONS,
            "exhibit h": SectionType.TERMS_CONDITIONS,
            "schedule": SectionType.RATE_SCHEDULE,
            "tariff": SectionType.RATE_SCHEDULE,
            "tariff sheet": SectionType.RATE_SCHEDULE,
            "rider sheet": SectionType.RIDER,
            "toc": SectionType.TABLE_OF_CONTENTS,
            "contents": SectionType.TABLE_OF_CONTENTS,
            "cover": SectionType.COVER_LETTER,
            "letter": SectionType.COVER_LETTER,
            "transmittal": SectionType.COVER_LETTER,
            "procedural": SectionType.PROCEDURAL,
            "procedural history": SectionType.PROCEDURAL,
            "certificate": SectionType.PROCEDURAL,
            "order": SectionType.PROCEDURAL,
            "rate_schedule": SectionType.RATE_SCHEDULE,
            "terms_conditions": SectionType.TERMS_CONDITIONS,
            "terms_and_conditions": SectionType.TERMS_CONDITIONS,
            "terms and conditions": SectionType.TERMS_CONDITIONS,
            "cover_letter": SectionType.COVER_LETTER,
            "table_of_contents": SectionType.TABLE_OF_CONTENTS,
            "unknown": SectionType.UNKNOWN,
        }
        clean = llm_type.lower().strip().rstrip(".")
        # First, try the direct constructor — handles all valid SectionType values
        try:
            return SectionType(clean)
        except ValueError:
            pass
        # Handle patterns like "Exhibit H", "Appendix A", "Attachment 1"
        if " " in clean:
            prefix = clean.split(" ")[0]
            if prefix in _TYPE_MAP:
                return _TYPE_MAP[prefix]
        return _TYPE_MAP.get(clean, SectionType.UNKNOWN)

    def _get_section_type(self, source_pdf: str, section_index: int) -> str:
        """Get the current deterministic section type for comparison."""
        existing = fetch_sections(self._db_path, source_pdf)
        for es in existing:
            if es.section_index == section_index:
                return (
                    es.section_type.value
                    if hasattr(es.section_type, 'value')
                    else str(es.section_type)
                )
        return "unknown"

    def _build_section_context(
        self, source_pdf: str, section_index: int,
    ) -> dict[str, Any] | None:
        """Build minimal context for a single section's LLM analysis.

        Only includes pages in the section's page range.
        """
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Get this section
            section = conn.execute(
                """SELECT section_index, start_page, end_page, section_type,
                          overall_confidence, leaf_numbers_json,
                          schedule_codes_json, rider_codes_json
                   FROM document_sections
                   WHERE source_pdf = ? AND section_index = ?""",
                (source_pdf, section_index),
            ).fetchone()

            if section is None:
                return None

            sp = section["start_page"]
            ep = section["end_page"]

            # Load pages in this section only
            pages = conn.execute(
                """SELECT page_number, text_content, metadata_json
                   FROM ncuc_page_artifacts
                   WHERE source_pdf = ?
                     AND page_number BETWEEN ? AND ?
                   ORDER BY page_number""",
                (source_pdf, sp, ep),
            ).fetchall()

            if not pages:
                return None

            # Document identity for signals
            identity = conn.execute(
                """SELECT schedule_codes_strong_json, rider_codes_strong_json,
                          leaf_numbers_json, detected_titles_json,
                          inferred_doc_type, profile_consensus_top
                   FROM document_identity
                   WHERE source_pdf = ?""",
                (source_pdf,),
            ).fetchone()
        finally:
            conn.close()

        # Build page summary
        page_lines: list[str] = []
        for row in pages:
            try:
                meta = json.loads(row["metadata_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            pn = row["page_number"]
            text = (row["text_content"] or "")[:_MAX_PAGE_TEXT_CHARS]
            text_clean = text.replace("\n", " ").replace("\r", " ")
            leaf = ", ".join(str(n) for n in (meta.get("extracted_leaf_nos") or []))
            codes = ", ".join(
                str(c) for c in (meta.get("extracted_schedule_codes") or [])
            )[:80]
            has_leaf = "Y" if meta.get("has_leaf_header") else "N"
            has_sched = "Y" if meta.get("has_schedule_heading") else "N"
            has_dollar = "Y" if "$" in text else "N"
            page_lines.append(
                f"  Pg {pn:3d} | leaf={has_leaf} sched={has_sched} $={has_dollar} "
                f"| leaves=[{leaf}] | codes=[{codes}]\n"
                f"         | text: {text_clean}"
            )

        # Doc signals
        sig_parts = []
        if identity:
            try:
                sched = json.loads(identity["schedule_codes_strong_json"] or "[]")
                if sched:
                    sig_parts.append(f"schedule_codes={sched[:4]}")
                rider = json.loads(identity["rider_codes_strong_json"] or "[]")
                if rider:
                    sig_parts.append(f"rider_codes={rider[:4]}")
                leaves = json.loads(identity["leaf_numbers_json"] or "[]")
                if leaves:
                    sig_parts.append(f"leaf_numbers={leaves[:4]}")
            except (json.JSONDecodeError, TypeError):
                pass
            if identity["profile_consensus_top"]:
                sig_parts.append(f"profile={identity['profile_consensus_top']}")
            if identity["inferred_doc_type"]:
                sig_parts.append(f"doc_type={identity['inferred_doc_type']}")

        return {
            "current_type": section["section_type"],
            "start_page": sp,
            "end_page": ep,
            "current_confidence": f"{section['overall_confidence']:.2f}",
            "doc_signals": "; ".join(sig_parts) if sig_parts else "none",
            "page_summary": "\n".join(page_lines),
        }
