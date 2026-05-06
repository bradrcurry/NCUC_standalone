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
import sqlite3
from pathlib import Path
from typing import Any

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
    "$/day",
    "kWh",
    "kW",
    "$",
    "¢",
    "%",
)


ALLOWED_CANDIDATE_STATUSES: tuple[str, ...] = (
    "candidate",
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
        max_text_chars: int = 4000,
    ) -> None:
        self._orch = orchestrator
        self._db_path = db_path
        self._role = role
        self._vlm_role = vlm_role
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
    ) -> list[dict[str, Any]]:
        """Select weak/empty parse attempts that are candidates for LLM extraction."""
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
                WHERE la.rn = 1
                  AND hd.id IS NOT NULL
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
        """Get document text from page artifacts."""
        if not source_pdf and not historical_document_id:
            return ""
        conn = sqlite3.connect(str(self._db_path))
        try:
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
            pass
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

    def extract_batch(
        self, candidates: list[dict[str, Any]], limit: int = 10
    ) -> list[CandidateRateExtraction]:
        """Run extraction on multiple candidates."""
        results: list[CandidateRateExtraction] = []
        for candidate in candidates[:limit]:
            try:
                extraction = self.extract_candidate(candidate)
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
            pass


def _read_text_file(path_value: str, max_chars: int) -> str:
    """Best-effort read of an extracted text file path."""
    try:
        path = Path(path_value)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception:
        return ""
