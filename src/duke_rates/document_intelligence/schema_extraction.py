"""
Schema-guided LLM fallback extraction (Phase 5.6).

For documents where the deterministic regex parse is weak/empty, text quality
is adequate, and the document likely contains a rate table, uses an LLM to
extract candidate rate rows.

All extractions are CANDIDATE rows only — they are NEVER merged into
production ``tariff_charges`` without validation. Also provides an optional
VLM pathway for table-layout failures.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from pydantic import BaseModel, Field

from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator

# ---------------------------------------------------------------------------
# Allowed enumerations
# ---------------------------------------------------------------------------


ALLOWED_CHARGE_TYPES: tuple[str, ...] = (
    "Basic Facilities Charge",
    "Energy Charge",
    "Demand Charge",
    "Rider Adjustment",
    "Fixed Monthly Charge",
    "Minimum Bill",
    "Seasonal Rate",
    "TOU Rate",
    "Lighting Charge",
    "Other",
)


ALLOWED_TOU_PERIODS: tuple[str, ...] = (
    "On-Peak",
    "Off-Peak",
    "Shoulder",
    "Super Off-Peak",
    "Critical Peak",
)

ALLOWED_UNITS: tuple[str, ...] = (
    "$/kWh",
    "¢/kWh",
    "$/kW",
    "$/month",
    "$/bill",
    "$/day",
    "kWh",
    "kW",
    "$",
    "¢",
    "%",
)


ALLOWED_CANDIDATE_STATUSES: tuple[str, ...] = (
    "candidate",
    "review_candidate",
    "validated",
    "rejected",
    "promoted",
)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class CandidateRateRow(BaseModel):
    """A single candidate rate row extracted by an LLM."""

    charge_type: str = Field(default="Other", description="Type of charge")
    season: str | None = Field(default=None, description="Summer, Winter, or null")
    tou_period: str | None = Field(
        default=None, description=f"One of: {', '.join(ALLOWED_TOU_PERIODS)}"
    )
    customer_class: str | None = Field(default=None, description="Residential, General Service, etc.")
    value: float = Field(default=0.0, description="Numeric rate value")
    unit: str = Field(default="", description=f"One of: {', '.join(ALLOWED_UNITS)}")
    source_quote: str = Field(default="", description="Exact source text")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Per-row confidence")


class DocumentSignals(BaseModel):
    utility: str = ""
    tariff_family: str = ""
    effective_date: str = ""
    leaf_number: str = ""
    is_redline: bool = False


class CandidateRateExtraction(BaseModel):
    """LLM-extracted candidate rate rows. NOT production data."""

    rate_rows: list[CandidateRateRow] = Field(default_factory=list)
    document_signals: DocumentSignals = Field(default_factory=DocumentSignals)
    extraction_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Staged extraction prompts (Phase 6E — iterative refinement)
# ---------------------------------------------------------------------------
#
# Why staged extraction:
#   The one-shot prompt asks the model to find rates AND classify charge_type
#   AND pick TOU period AND assign customer_class AND choose the unit — all
#   on up to 4000 chars at once. Small local models drop classification
#   quality when the decision space is wide (8 charge types × 5 TOU periods
#   × 10 units). Splitting the task into "find lines" then "classify each
#   line individually" each get a tiny decision space and the model performs
#   substantially better.
#
# Each stage is a separate LLM call. With max_tokens=600 and temperature=0
# each stage runs in 3-8s on an 8GB GPU. A 3-row document costs ~1 line-
# finding call + 3 classify calls = 4 small calls instead of 1 big call,
# but each call has higher accuracy.

_STAGE_LINE_FINDER_PROMPT = """\
You are scanning a tariff document. Return ONLY the lines that contain a
specific numeric rate value (a dollar amount, cents per kWh, or kW demand
charge).

## Document signals:
- Utility: {utility}
- Tariff family: {tariff_family}
- Effective date: {effective_date}
- Leaf number: {leaf_number}

## Document text:
```
{document_text}
```

## What counts as a rate line:
- Lines with $X.XX values for energy, demand, fixed, or rider charges
- Lines with X.XXX¢ or X.XXX cents per kWh
- Lines with "per kWh", "per kW", "per month", "per day" near a number

## What does NOT count:
- Procedural text ("rates are determined by", "see Appendix A")
- Definitions and applicability clauses
- One-time bill credits, refunds, deposits
- Table-of-contents entries
- Date ranges, docket numbers, page numbers

## Instructions:
1. Return EVERY line with a quantifiable rate value, verbatim, in document order.
2. Include the full line — do not truncate mid-sentence.
3. If the document has NO rate lines (e.g. it's a program description or
   terms-and-conditions section), return an empty list.

Respond with a single JSON object: {{"rate_lines": ["...", "...", ...]}}.
No other text."""


_STAGE_CLASSIFY_LINE_PROMPT = """\
Classify one tariff rate line into a structured row.

## Source line:
"{rate_line}"

## Context (one paragraph of surrounding text):
"{context}"

## Document signals:
- Utility: {utility}
- Tariff family: {tariff_family}
- Customer class hint: {customer_class_hint}

## Decision rules:
1. charge_type MUST be one of: {allowed_charge_types}
   - "Basic Facilities Charge" / "Fixed Monthly Charge" — flat $ amount per month
   - "Energy Charge" — per-kWh consumption charge
   - "Demand Charge" — per-kW peak demand charge
   - "Rider Adjustment" — fuel adjustment, REPS, etc. (cents/kWh additions)
   - "Minimum Bill" — minimum monthly charge floor
   - "TOU Rate" — only if the line itself says "On-Peak"/"Off-Peak"
   - "Lighting Charge" — outdoor / street lighting per-fixture rates
   - "Other" — only if NONE of the above fit
2. unit MUST be one of: {allowed_units}
   - If the line says "per kWh" → "$/kWh" or "¢/kWh"
   - If the line says "per month" with no "/kWh" → "$/month"
   - If the line says "per kW" → "$/kW"
   - NEVER emit bare "$" — pick the qualified form
3. value: the numeric value, no units (e.g. 10.369 for "10.369¢ per kWh")
4. season: "Summer" / "Winter" / null — only if the line explicitly says so
5. tou_period: one of {allowed_tou_periods} or null — only if line says so

Respond with a single JSON object matching the schema. Set confidence
based on how unambiguously the line maps to a single charge_type."""


_STAGE_LINE_FINDER_PROMPT_HINT = (
    "Lines containing $ or ¢ values with rate-bearing context"
)


# ---------------------------------------------------------------------------
# Stage schemas (Pydantic)
# ---------------------------------------------------------------------------


class RateLineList(BaseModel):
    """Output of stage 1: just the verbatim rate-bearing lines."""

    rate_lines: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Original one-shot prompt (Phase 5.6, kept for fallback)
# ---------------------------------------------------------------------------

_EXTRACTION_SYSTEM_PROMPT = """\
You are a tariff rate extraction specialist for the North Carolina Utilities Commission (NCUC).
Your task is to extract rate charges from a tariff document that a deterministic parser
failed to process.

## Document signals:
- Utility: {utility}
- Tariff family: {tariff_family}
- Effective date: {effective_date}
- Leaf number: {leaf_number}
- Is redline: {is_redline}

## Document text:
```
{document_text}
```

## Instructions:
1. Extract every rate row you can find. Include ALL charges visible in the text.
2. For each row, provide:
   - charge_type: one of {allowed_charge_types}
   - season: "Summer", "Winter", or null if not seasonal
   - tou_period: one of {allowed_tou_periods} or null
   - customer_class: "Residential", "General Service", etc. or null
   - value: numeric rate value (just the number, no units)
   - unit: one of {allowed_units}
   - source_quote: the EXACT text from the document that contains this rate
   - confidence: 0.0-1.0 per row based on clarity of the source text
3. Include a document_signals object with any metadata you can detect.
4. extraction_confidence: overall 0.0-1.0 confidence in the extraction quality.
5. List any warnings (ambiguous text, missing units, unclear charge types).
6. Do NOT invent charges — if you can't find a clear rate, return empty rate_rows.
7. If the document is a redline/proposed tariff, note it in warnings.

Respond with a single JSON object matching the required schema. No other text."""


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


class SchemaGuidedExtractor:
    """Schema-guided LLM fallback extraction for hard parse cases.

    Parameters
    ----------
    orchestrator : OllamaOrchestrator
        Phase 2.5 orchestrator. Must have ``structured_rate_extraction`` role.
    db_path : Path
        Path to the SQLite database.
    role : str
        Orchestrator role (default ``"structured_rate_extraction"``).
    vlm_role : str
        VLM role for layout/table extraction (default ``"layout_table_extraction"``).
    max_text_chars : int
        Truncate document text to this many characters (default 4000).
    """

    def __init__(
        self,
        orchestrator: OllamaOrchestrator,
        db_path: Path,
        *,
        role: str = "structured_rate_extraction",
        vlm_role: str = "layout_table_extraction",
        classify_role: str = "structured_rate_classify",
        max_text_chars: int = 4000,
    ) -> None:
        self._orch = orchestrator
        self._db_path = db_path
        self._role = role
        self._vlm_role = vlm_role
        # Stage 3 (per-line classify) uses a different role than stage 2
        # (find-lines). 2026-05 benchmark showed qwen3:8b dominates on
        # classify while gemma4 dominates on find-lines — model-per-stage
        # gives 100% valid output on both.
        self._classify_role = classify_role
        self._max_text_chars = max_text_chars

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select_extraction_candidates(
        self,
        *,
        limit: int = 10,
        historical_document_id: int | None = None,
        profile: str | None = None,
        family: str | None = None,
        doc_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Select weak/empty parse attempts that are candidates for LLM extraction.

        When no specific filter (*historical_document_id*, *profile*,
        *family*) is provided, and *doc_types* is not passed, the selection
        defaults to tariff-relevant document types only
        (``compliance_tariff_bundle``, ``single_rate_schedule``).
        """
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            params: list[Any] = []
            cte_where = ""
            outer_where = ""

            if historical_document_id:
                cte_where += " AND CAST(json_extract(pal.metadata_json, '$.historical_document_id') AS INTEGER) = ?"
                params.append(historical_document_id)
            if profile:
                cte_where += " AND pal.parser_profile = ?"
                params.append(profile)

            # Doc-type filter: when no specific target is given, focus on
            # documents that are actually rate schedules / tariff bundles.
            has_specific_target = bool(historical_document_id or profile or family)
            _doc_types = doc_types
            if _doc_types is None and not has_specific_target:
                _doc_types = ["compliance_tariff_bundle", "single_rate_schedule"]

            doc_type_join = ""
            doc_type_where = ""
            if _doc_types:
                doc_type_join = (
                    "LEFT JOIN document_identity di ON di.source_pdf = la.source_pdf"
                )
                placeholders = ", ".join("?" for _ in _doc_types)
                doc_type_where = (
                    f"AND di.inferred_doc_type IN ({placeholders})"
                )
                params.extend(_doc_types)

            # family goes after doc_types in params to match SQL binding order:
            # {doc_type_where} appears before {outer_where} in the query.
            if family:
                outer_where += " AND lr.family_key = ?"
                params.append(family)

            rows = conn.execute(
                f"""
                WITH latest AS (
                    SELECT pal.*,
                           CAST(
                               COALESCE(json_extract(pal.metadata_json, '$.historical_document_id'), '0')
                               AS INTEGER
                           ) AS parsed_historical_document_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY pal.source_pdf, pal.page_start, pal.page_end
                               ORDER BY pal.id DESC
                           ) AS rn
                    FROM parse_attempt_logs pal
                    WHERE pal.parser_stage = 'historical_bulk'
                      AND (pal.charge_count = 0
                           OR json_extract(pal.metadata_json, '$.outcome_quality') = 'weak')
                      AND pal.status NOT LIKE 'skipped_%'
                      {cte_where}
                ),
                latest_run AS (
                    SELECT hpr.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY hpr.historical_document_id
                               ORDER BY hpr.completed_at DESC
                           ) AS rn
                    FROM historical_processing_runs hpr
                )
                SELECT la.id AS parse_attempt_id,
                       la.source_pdf,
                       la.parser_profile,
                       la.effective_date,
                       la.charge_count,
                       la.status,
                       la.metadata_json,
                       COALESCE(NULLIF(la.parsed_historical_document_id, 0), lr.historical_document_id) AS historical_document_id,
                       COALESCE(hd.family_key, lr.family_key) AS family_key,
                       hd.raw_text_path
                FROM latest la
                LEFT JOIN latest_run lr
                  ON lr.rn = 1
                 AND lr.historical_document_id = la.parsed_historical_document_id
                LEFT JOIN historical_documents hd
                  ON hd.id = COALESCE(NULLIF(la.parsed_historical_document_id, 0), lr.historical_document_id)
                {doc_type_join}
                WHERE la.rn = 1
                  AND hd.id IS NOT NULL
                  {doc_type_where}
                  AND (
                      EXISTS (
                          SELECT 1
                          FROM ncuc_page_artifacts pa
                          WHERE pa.source_pdf = la.source_pdf
                            AND COALESCE(pa.text_length, LENGTH(COALESCE(pa.text_content, ''))) >= 50
                      )
                      OR COALESCE(hd.raw_text_path, '') != ''
                  )
                  {outer_where}
                  AND la.id NOT IN (
                      SELECT parse_attempt_id FROM llm_parse_diagnostics
                      WHERE parse_attempt_id IS NOT NULL
                        AND recommended_action = 'schema_extract_candidate'
                  )
                  AND la.source_pdf NOT IN (
                      SELECT DISTINCT source_pdf FROM llm_candidate_rate_extractions
                      WHERE extraction_confidence >= 0.5
                  )
                ORDER BY la.charge_count ASC
                LIMIT ?
                """,
                tuple(params + [max(limit * 5, limit)]),
            ).fetchall()
            candidates: list[dict[str, Any]] = []
            for row in rows:
                candidate = dict(row)
                text = self.get_document_text(
                    candidate.get("source_pdf", ""),
                    candidate.get("historical_document_id"),
                )
                if len(text.strip()) >= 50:
                    candidates.append(candidate)
                if len(candidates) >= limit:
                    break
            return candidates
        finally:
            conn.close()

    def get_document_signals(self, candidate: dict[str, Any]) -> DocumentSignals:
        """Extract document metadata signals for the LLM prompt."""
        metadata = {}
        try:
            raw = candidate.get("metadata_json", "{}")
            metadata = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            pass

        return DocumentSignals(
            utility=str(metadata.get("utility") or ""),
            tariff_family=str(candidate.get("family_key") or ""),
            effective_date=str(candidate.get("effective_date") or ""),
            leaf_number=str(metadata.get("leaf_number") or ""),
            is_redline=bool(metadata.get("is_redline", False)),
        )

    def get_document_text(
        self, source_pdf: str, historical_document_id: int | None = None
    ) -> str:
        """Get document text, preferring section-aware text when available.

        If document_sections has high-confidence rate_schedule/rider sections,
        their page text is used instead of the first N pages — giving the LLM
        focused tariff content rather than cover letters or boilerplate.
        """
        if not source_pdf and not historical_document_id:
            return ""
        conn = sqlite3.connect(str(self._db_path))
        try:
            # Try section-aware text first: use pages from the best
            # rate_schedule/rider section (highest confidence, most pages).
            section_pages = conn.execute(
                """
                SELECT pa.text_content
                FROM document_sections ds
                JOIN ncuc_page_artifacts pa
                  ON pa.source_pdf = ds.source_pdf
                 AND pa.page_number BETWEEN ds.start_page AND ds.end_page
                WHERE ds.source_pdf = ?
                  AND ds.section_type IN ('rate_schedule', 'rider')
                  AND ds.overall_confidence >= 0.5
                ORDER BY ds.overall_confidence DESC,
                         (ds.end_page - ds.start_page + 1) DESC
                """,
                (source_pdf,),
            ).fetchall()
            if section_pages:
                text = "\n".join(p[0] or "" for p in section_pages)
                if text.strip():
                    return text[: self._max_text_chars]

            # Fallback: first 15 pages
            pages = conn.execute(
                """
                SELECT pa.text_content
                FROM ncuc_page_artifacts pa
                WHERE pa.source_pdf = ?
                ORDER BY pa.page_number
                LIMIT 15
                """,
                (source_pdf,),
            ).fetchall()
            if pages:
                text = "\n".join(p[0] or "" for p in pages)
                if text.strip():
                    return text[: self._max_text_chars]

            if historical_document_id:
                row = conn.execute(
                    """
                    SELECT raw_text_path
                    FROM historical_documents
                    WHERE id = ?
                    """,
                    (historical_document_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT raw_text_path
                    FROM historical_documents
                    WHERE local_path = ? OR canonical_url = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (source_pdf, source_pdf),
                ).fetchone()
            if row and row[0]:
                return _read_text_file(row[0], self._max_text_chars)
        except Exception:
            logger.debug("get_document_text failed for %s", source_pdf, exc_info=True)
        finally:
            conn.close()
        return ""

    def extract_candidate(self, candidate: dict[str, Any]) -> CandidateRateExtraction | None:
        """Run schema-guided LLM extraction on one candidate. Never raises."""
        source_pdf = candidate.get("source_pdf", "")
        doc_signals = self.get_document_signals(candidate)
        text = self.get_document_text(
            source_pdf, candidate.get("historical_document_id")
        )

        if not text or len(text.strip()) < 50:
            return CandidateRateExtraction(
                document_signals=doc_signals,
                warnings=["Insufficient text for LLM extraction"],
            )

        prompt = _EXTRACTION_SYSTEM_PROMPT.format(
            utility=doc_signals.utility or "(unknown)",
            tariff_family=doc_signals.tariff_family or "(unknown)",
            effective_date=doc_signals.effective_date or "(unknown)",
            leaf_number=doc_signals.leaf_number or "(unknown)",
            is_redline=str(doc_signals.is_redline),
            document_text=text,
            allowed_charge_types=", ".join(ALLOWED_CHARGE_TYPES),
            allowed_tou_periods=", ".join(ALLOWED_TOU_PERIODS),
            allowed_units=", ".join(ALLOWED_UNITS),
        )

        try:
            run_result = self._orch.generate_json(
                role=self._role,
                prompt=prompt,
                schema=CandidateRateExtraction,
                subject_kind="parse_attempt",
                subject_id=str(candidate.get("parse_attempt_id", "0")),
                stage="schema_rate_extraction",
            )
        except Exception:
            return None

        if run_result.status not in ("ok", "fallback_used"):
            return CandidateRateExtraction(
                document_signals=doc_signals,
                warnings=[f"LLM call failed: {run_result.status}"],
            )

        extraction: CandidateRateExtraction = run_result.result

        # Validate charge types
        for row in extraction.rate_rows:
            if row.charge_type not in ALLOWED_CHARGE_TYPES:
                row.charge_type = "Other"
            if row.tou_period and row.tou_period not in ALLOWED_TOU_PERIODS:
                row.tou_period = None
            if row.unit and row.unit not in ALLOWED_UNITS:
                row.unit = ""

        extraction.extraction_confidence = round(extraction.extraction_confidence, 4)

        # Persist as CANDIDATE (never production)
        self._persist_extraction(
            extraction,
            candidate,
            run_result.model or "unknown",
        )

        return extraction

    # ------------------------------------------------------------------
    # Staged extraction (Phase 6E — iterative refinement)
    # ------------------------------------------------------------------

    # Skip docs where rate-relevant text has fewer than this many $ or ¢
    # tokens — they're almost certainly brochures or program descriptions.
    _STAGE_MIN_RATE_TOKENS = 3

    # Cap on lines passed to the per-line classifier (limits LLM-call count
    # per doc). Bigger docs usually have a few duplicated rate lines anyway.
    _STAGE_MAX_LINES = 15

    def extract_candidate_staged(
        self, candidate: dict[str, Any]
    ) -> CandidateRateExtraction | None:
        """Staged LLM extraction: filter → find-lines → classify-per-line.

        Three-stage pipeline that replaces the one-shot prompt:
        1. **Filter**: skip docs with insufficient rate tokens or with a
           prior "no quantifiable rates" warning from a recent extraction.
        2. **Find lines**: ask the LLM to return only verbatim rate-bearing
           lines. Tiny output shape (one list of strings).
        3. **Classify each line**: one call per line with a focused prompt
           that has a much smaller decision space than the one-shot
           extraction.

        Returns the same CandidateRateExtraction shape as
        ``extract_candidate`` so callers and the persistence path don't
        change. Warnings collected from each stage are aggregated.
        """
        source_pdf = candidate.get("source_pdf", "")
        doc_signals = self.get_document_signals(candidate)
        text = self.get_document_text(
            source_pdf, candidate.get("historical_document_id")
        )

        warnings: list[str] = []

        if not text or len(text.strip()) < 50:
            return CandidateRateExtraction(
                document_signals=doc_signals,
                warnings=["Insufficient text for LLM extraction"],
            )

        # --- Stage 1: deterministic filter ---
        rate_token_count = text.count("$") + text.count("¢") + text.count("c/")
        if rate_token_count < self._STAGE_MIN_RATE_TOKENS:
            extraction = CandidateRateExtraction(
                document_signals=doc_signals,
                extraction_confidence=0.05,
                warnings=[
                    f"Skipped at stage 1: only {rate_token_count} $/¢ tokens "
                    "in rate-relevant text (likely brochure or T&C section)"
                ],
            )
            self._persist_extraction(extraction, candidate, "filtered")
            return extraction

        # Check prior warnings for this PDF — if a recent extraction warned
        # "no quantifiable rates" / "program description", don't burn LLM time.
        prior_warning = self._fetch_prior_skip_warning(source_pdf)
        if prior_warning:
            extraction = CandidateRateExtraction(
                document_signals=doc_signals,
                extraction_confidence=0.05,
                warnings=[
                    f"Skipped at stage 1: prior extraction warned "
                    f"({prior_warning[:120]})"
                ],
            )
            self._persist_extraction(extraction, candidate, "filtered-by-prior")
            return extraction

        # --- Stage 2: find rate-bearing lines ---
        find_prompt = _STAGE_LINE_FINDER_PROMPT.format(
            utility=doc_signals.utility or "(unknown)",
            tariff_family=doc_signals.tariff_family or "(unknown)",
            effective_date=doc_signals.effective_date or "(unknown)",
            leaf_number=doc_signals.leaf_number or "(unknown)",
            document_text=text,
        )

        try:
            find_result = self._orch.generate_json(
                role=self._role,
                prompt=find_prompt,
                schema=RateLineList,
                subject_kind="parse_attempt",
                subject_id=str(candidate.get("parse_attempt_id", "0")),
                stage="staged_extract_find_lines",
            )
        except Exception as exc:
            return CandidateRateExtraction(
                document_signals=doc_signals,
                warnings=[f"Stage 2 line-finder raised: {exc}"],
            )

        if find_result.status not in ("ok", "fallback_used"):
            return CandidateRateExtraction(
                document_signals=doc_signals,
                warnings=[f"Stage 2 line-finder failed: {find_result.status}"],
            )

        line_output: RateLineList = find_result.result
        rate_lines = [
            ln.strip() for ln in line_output.rate_lines
            if ln and ln.strip()
        ]

        # Dedupe while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for ln in rate_lines:
            key = ln.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(ln)
        rate_lines = deduped[: self._STAGE_MAX_LINES]

        if not rate_lines:
            warnings.append(
                "Stage 2: line-finder returned no rate-bearing lines"
            )
            extraction = CandidateRateExtraction(
                document_signals=doc_signals,
                rate_rows=[],
                extraction_confidence=0.1,
                warnings=warnings,
            )
            self._persist_extraction(
                extraction, candidate, find_result.model or "unknown",
            )
            return extraction

        # --- Stage 3: classify each line ---
        rows: list[CandidateRateRow] = []
        customer_class_hint = self._derive_customer_class_hint(doc_signals)
        for line in rate_lines:
            context = self._find_line_context(text, line, window=200)
            classify_prompt = _STAGE_CLASSIFY_LINE_PROMPT.format(
                rate_line=line[:400],
                context=context[:600],
                utility=doc_signals.utility or "(unknown)",
                tariff_family=doc_signals.tariff_family or "(unknown)",
                customer_class_hint=customer_class_hint,
                allowed_charge_types=", ".join(ALLOWED_CHARGE_TYPES),
                allowed_tou_periods=", ".join(ALLOWED_TOU_PERIODS),
                allowed_units=", ".join(ALLOWED_UNITS),
            )

            try:
                row_result = self._orch.generate_json(
                    role=self._classify_role,
                    prompt=classify_prompt,
                    schema=CandidateRateRow,
                    subject_kind="parse_attempt",
                    subject_id=str(candidate.get("parse_attempt_id", "0")),
                    stage="staged_extract_classify",
                )
            except Exception:
                continue

            if row_result.status not in ("ok", "fallback_used"):
                continue

            row: CandidateRateRow = row_result.result
            # Defensive normalization
            if row.charge_type not in ALLOWED_CHARGE_TYPES:
                row.charge_type = "Other"
            if row.tou_period and row.tou_period not in ALLOWED_TOU_PERIODS:
                row.tou_period = None
            if row.unit and row.unit not in ALLOWED_UNITS:
                row.unit = ""
            # Reject obvious nonsense (zero value when source_quote has digits)
            if row.value == 0.0 and any(
                ch.isdigit() for ch in (row.source_quote or line)
            ):
                # Try to recover the numeric value from the source line.
                recovered = self._extract_numeric_from_line(line)
                if recovered is not None:
                    row.value = recovered
                else:
                    continue
            # Use the original line as source_quote if the model didn't echo it.
            if not (row.source_quote or "").strip():
                row.source_quote = line[:300]
            rows.append(row)

        # Aggregate per-row confidence into overall extraction confidence.
        if rows:
            avg_conf = sum(r.confidence for r in rows) / len(rows)
        else:
            avg_conf = 0.0

        # Flag low-confidence extractions for human review.
        if avg_conf <= 0.5 and rows:
            warnings.append(
                f"Low average confidence ({avg_conf:.2f}) — flag for review"
            )

        # If 30%+ of classified rows ended up as "Other", that's a quality
        # signal worth surfacing.
        if rows:
            other_pct = sum(1 for r in rows if r.charge_type == "Other") / len(rows)
            if other_pct > 0.3:
                warnings.append(
                    f"{other_pct:.0%} of rows classified as 'Other' — "
                    "review charge_type quality"
                )

        extraction = CandidateRateExtraction(
            rate_rows=rows,
            document_signals=doc_signals,
            extraction_confidence=round(avg_conf, 4),
            warnings=warnings,
        )

        self._persist_extraction(
            extraction, candidate, find_result.model or "unknown",
        )
        return extraction

    def _fetch_prior_skip_warning(self, source_pdf: str) -> str | None:
        """Return a prior 'no quantifiable rates' warning, if any.

        Lets stage 1 short-circuit when we already paid the LLM cost on a
        previous overnight run and learned the doc has no rates.
        """
        if not source_pdf:
            return None
        try:
            conn = sqlite3.connect(str(self._db_path))
            row = conn.execute(
                """
                SELECT warnings_json FROM llm_candidate_rate_extractions
                WHERE source_pdf = ?
                  AND rate_rows_json = '[]'
                  AND warnings_json LIKE '%no explicit%'
                ORDER BY id DESC LIMIT 1
                """,
                (source_pdf,),
            ).fetchone()
            conn.close()
            if not row:
                return None
            warnings = json.loads(row[0] or "[]")
            for w in warnings:
                lower = (w or "").lower()
                if (
                    "no explicit" in lower
                    or "no quantifiable" in lower
                    or "program description" in lower
                    or "terms and conditions" in lower
                ):
                    return w
        except Exception:
            return None
        return None

    @staticmethod
    def _derive_customer_class_hint(doc_signals: DocumentSignals) -> str:
        """Infer a customer-class hint from tariff_family for the classifier."""
        family = (doc_signals.tariff_family or "").lower()
        if not family:
            return "(unknown — infer from line context)"
        if any(t in family for t in ("res", "rs", "residential")):
            return "Residential"
        if any(t in family for t in ("sgs", "small general")):
            return "Small General Service"
        if any(t in family for t in ("lgs", "large general", "hp ")):
            return "Large General Service"
        if any(t in family for t in ("mgs", "medium")):
            return "Medium General Service"
        if "light" in family:
            return "Outdoor Lighting"
        return f"({family} — infer customer class from this code)"

    @staticmethod
    def _find_line_context(text: str, line: str, *, window: int) -> str:
        """Return *window* chars around *line* in *text* for classification context."""
        if not line or not text:
            return ""
        idx = text.find(line[:80])  # match by prefix to tolerate truncation
        if idx < 0:
            # Try a looser match on the first 30 chars
            idx = text.find(line[:30]) if len(line) >= 30 else -1
        if idx < 0:
            return ""
        start = max(0, idx - window)
        end = min(len(text), idx + len(line) + window)
        return text[start:end]

    _NUMERIC_LINE_RE = re.compile(r"(\d+(?:\.\d+)?)")

    @classmethod
    def _extract_numeric_from_line(cls, line: str) -> float | None:
        """Pull the last numeric value from a rate line, if any.

        Anchors that look like leaf numbers ("Leaf No. 331") would normally
        match the first number, but rate lines almost always have the value
        as the last (or only) decimal number — preferring decimals over bare
        integers avoids picking up leaf numbers and dates.
        """
        if not line:
            return None
        candidates = cls._NUMERIC_LINE_RE.findall(line)
        if not candidates:
            return None
        decimal_first = [c for c in candidates if "." in c]
        chosen = decimal_first[-1] if decimal_first else candidates[-1]
        try:
            return float(chosen)
        except ValueError:
            return None

    def extract_batch(
        self,
        candidates: list[dict[str, Any]],
        limit: int = 10,
        *,
        staged: bool = False,
    ) -> list[CandidateRateExtraction]:
        """Run extraction on multiple candidates.

        When *staged* is True, uses the three-stage iterative pipeline
        (filter → find lines → classify per line) instead of the
        one-shot prompt. Slower per doc but better classification
        accuracy and explicit filter behavior for non-tariff docs.
        """
        results: list[CandidateRateExtraction] = []
        method = (
            self.extract_candidate_staged if staged else self.extract_candidate
        )
        for candidate in candidates[:limit]:
            try:
                extraction = method(candidate)
                if extraction:
                    results.append(extraction)
            except Exception:
                continue
        return results

    def extract_with_layout(
        self, candidate: dict[str, Any], page_image_path: str | None = None
    ) -> CandidateRateExtraction | None:
        """VLM-based extraction for table-layout failures.

        Uses the ``layout_table_extraction`` role (qwen3-vl:4b) to extract
        rate rows from a page image. Only for failures classified as
        ``table_layout``.
        """
        # Probe VLM availability
        ok, err = self._orch.health_probe(self._vlm_role)
        if not ok:
            return CandidateRateExtraction(
                document_signals=self.get_document_signals(candidate),
                warnings=[f"VLM not available: {err}"],
            )

        doc_signals = self.get_document_signals(candidate)
        text = self.get_document_text(
            candidate.get("source_pdf", ""),
            candidate.get("historical_document_id"),
        )

        if not text or len(text.strip()) < 50:
            return CandidateRateExtraction(
                document_signals=doc_signals,
                warnings=["Insufficient text for VLM extraction"],
            )

        # VLM extraction uses the same prompt schema but the model can see images
        prompt = _EXTRACTION_SYSTEM_PROMPT.format(
            utility=doc_signals.utility or "(unknown)",
            tariff_family=doc_signals.tariff_family or "(unknown)",
            effective_date=doc_signals.effective_date or "(unknown)",
            leaf_number=doc_signals.leaf_number or "(unknown)",
            is_redline=str(doc_signals.is_redline),
            document_text=text,
            allowed_charge_types=", ".join(ALLOWED_CHARGE_TYPES),
            allowed_tou_periods=", ".join(ALLOWED_TOU_PERIODS),
            allowed_units=", ".join(ALLOWED_UNITS),
        )

        # Additional context for VLM
        if page_image_path:
            prompt += f"\n\nA page image is available at: {page_image_path}\nUse the visual layout to resolve any table structure ambiguities in the text above."

        try:
            run_result = self._orch.generate_json(
                role=self._vlm_role,
                prompt=prompt,
                schema=CandidateRateExtraction,
                subject_kind="parse_attempt",
                subject_id=str(candidate.get("parse_attempt_id", "0")),
                stage="schema_rate_extraction_vlm",
            )
        except Exception:
            return None

        if run_result.status not in ("ok", "fallback_used"):
            return None

        extraction: CandidateRateExtraction = run_result.result
        extraction.extraction_confidence = round(extraction.extraction_confidence, 4)

        self._persist_extraction(
            extraction,
            candidate,
            run_result.model or "unknown",
        )

        return extraction

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _persist_extraction(
        self,
        extraction: CandidateRateExtraction,
        candidate: dict[str, Any],
        model: str,
    ) -> None:
        """Write candidate extraction to llm_candidate_rate_extractions. Best-effort."""
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute(
                """
                INSERT INTO llm_candidate_rate_extractions
                    (historical_document_id, source_pdf, rate_rows_json,
                     document_signals_json, extraction_confidence, warnings_json,
                     model, model_role, prompt_version, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.get("historical_document_id"),
                    candidate.get("source_pdf", ""),
                    json.dumps([row.model_dump() for row in extraction.rate_rows]),
                    json.dumps(extraction.document_signals.model_dump()),
                    extraction.extraction_confidence,
                    json.dumps(extraction.warnings),
                    model,
                    self._role,
                    "v1",
                    "candidate",
                ),
            )
            conn.commit()
            conn.close()
        except Exception:
            logger.warning("_persist_extraction failed for %s", candidate.get("source_pdf"), exc_info=True)


def _read_text_file(path_value: str, max_chars: int) -> str:
    """Best-effort read of an extracted text file path."""
    try:
        path = Path(path_value)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception:
        return ""
