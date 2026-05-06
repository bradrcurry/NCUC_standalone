"""
LLM-assisted parse failure diagnosis (Phase 5.6).

Selects weak, empty, low-confidence, or anomalous parse attempts from
``parse_attempt_logs``, sends structured context to an LLM, and persists
a root-cause diagnosis with evidence and recommended actions.

LLM outputs are ADVISORY only — they do not modify parser code or
production charge outputs.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator

# ---------------------------------------------------------------------------
# Allowed enumerations (must match the LLM prompt)
# ---------------------------------------------------------------------------

ALLOWED_FAILURE_TYPES: tuple[str, ...] = (
    "wrong_family",
    "wrong_profile",
    "ocr_noise",
    "table_layout",
    "missing_effective_date",
    "bundled_document",
    "redline_or_proposed",
    "no_rate_table",
    "partial_span",
    "normalization_gap",
    "regex_gap",
    "unknown",
)

ALLOWED_RECOMMENDED_ACTIONS: tuple[str, ...] = (
    "retry_profile",
    "reroute_family",
    "apply_normalization",
    "suggest_regex",
    "route_to_vlm",
    "split_span",
    "bind_effective_date",
    "schema_extract_candidate",
    "human_review",
    "acquire_missing_pdf",
)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class DiagnosisEvidence(BaseModel):
    kind: str = Field(description="text_quote | parser_signal | metadata_signal")
    value: str = Field(description="The evidence content")


class ParseFailureDiagnosis(BaseModel):
    """Strict JSON output the LLM must produce for a parse failure diagnosis."""

    failure_type: str = Field(
        default="unknown",
        description=f"Root cause category. One of: {', '.join(ALLOWED_FAILURE_TYPES)}",
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: list[DiagnosisEvidence] = Field(default_factory=list)
    recommended_action: str = Field(
        default="human_review",
        description=f"One of: {', '.join(ALLOWED_RECOMMENDED_ACTIONS)}",
    )
    notes: str = Field(default="", description="Short explanation of the diagnosis")

    @model_validator(mode="before")
    @classmethod
    def normalize_model_output(cls, data: Any) -> Any:
        """Accept common local-LLM JSON variants without weakening enums."""
        if not isinstance(data, dict):
            return data

        # Some Ollama JSON-mode responses wrap the object in {"response": {...}}.
        response = data.get("response")
        if isinstance(response, dict):
            merged = dict(response)
            for key, value in data.items():
                if key != "response" and key not in merged:
                    merged[key] = value
            data = merged

        evidence = data.get("evidence")
        if isinstance(evidence, list):
            normalized_evidence: list[Any] = []
            for item in evidence:
                if isinstance(item, str):
                    normalized_evidence.append(
                        {"kind": "text_quote", "value": item}
                    )
                else:
                    normalized_evidence.append(item)
            data["evidence"] = normalized_evidence

        if not data.get("notes"):
            for key in ("notes", "explanation", "rationale", "reasoning"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    data["notes"] = value.strip()
                    break

        return data


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

_DIAGNOSIS_SYSTEM_PROMPT = """\
You are a tariff document parsing diagnostician for the North Carolina Utilities Commission (NCUC).
Your task is to diagnose WHY a deterministic regex parser failed to extract charges from a tariff document.

## Document context:
- Document: {document_path}
- Tariff family: {family_key}
- Parser profile used: {parser_profile}
- Effective date: {effective_date}
- Parser status: {parser_status}
- Parser confidence: {parser_confidence}
- Charges found: {charge_count}
- Expected charges (family peak): {expected_charges}

## Parser evidence (ranked candidate profiles):
{parser_evidence}

## Document text excerpt (first ~2000 chars):
```
{document_text}
```

## OCR / text quality:
{text_quality}

## Failure type definitions (pick the SINGLE best fit):
- **regex_gap**           — the document text is clean and the rate values are
                            clearly present, but no current parser regex
                            matches the way they're written. Fixable by adding
                            a new regex pattern.
- **normalization_gap**   — rate values are present but in a non-canonical form
                            the parser doesn't normalize: e.g. "10.5¢" vs
                            "$0.105", thousands-separator commas, mixed units
                            within the same line. Fixable by adding a textual
                            normalization rule BEFORE regex matching.
- **ocr_noise**           — the rate values exist in the source PDF but OCR
                            mangled them: ligatures (fi → "ﬁ"), zero/letter
                            confusion (0 vs O, 1 vs l), trailing-space
                            artifacts, broken-up tokens. Distinct from
                            normalization_gap because the underlying issue is
                            the OCR pass, not the parser.
- **table_layout**        — rates ARE in the document but inside a table whose
                            structure the parser can't traverse (multi-row
                            headers, merged cells, multi-column splits).
- **missing_effective_date** — parser could not determine which date version
                            of the tariff this is.
- **wrong_profile**       — the parser used the wrong rule set for this doc;
                            another profile would have extracted these rates.
- **wrong_family**        — the tariff family classification is wrong (e.g.
                            this is RES-28 but was filed under SGS).
- **bundled_document**    — the file contains multiple tariffs in one PDF and
                            the parser only saw one section.
- **redline_or_proposed** — this is a markup/redline copy or a proposed (not
                            approved) tariff — should not have been parsed.
- **no_rate_table**       — there genuinely are no charges in this document
                            (cover sheet, certificate of service, etc.).
- **partial_span**        — parser extracted a partial span but missed lines.
- **unknown**             — the evidence is insufficient to choose any of the
                            above with confidence.

## Instructions:
1. Identify the SINGLE most likely root cause for the parse failure.
2. Choose failure_type ONLY from the definitions above.
3. Choose recommended_action ONLY from this list: {allowed_actions}
4. Provide 1-3 pieces of evidence — quote source text where possible.
5. **When you see OCR artifacts (ligatures, character substitutions, broken
   spacing) in the document text, prefer `ocr_noise` over `regex_gap`.**
6. **When rates are clearly present but in a non-canonical form (cents
   notation, unusual unit strings), prefer `normalization_gap` over
   `regex_gap`.**
7. Return "unknown" if the evidence is insufficient.
8. Do NOT invent labels or actions outside the allowed lists.
9. Confidence must be 0.0-1.0. Use 0.0 when you have no signal.

Respond with a single JSON object matching the required schema. No other text."""


# ---------------------------------------------------------------------------
# Diagnoser
# ---------------------------------------------------------------------------


class ParseFailureDiagnoser:
    """LLM-assisted parse failure diagnosis.

    Parameters
    ----------
    orchestrator : OllamaOrchestrator
        Phase 2.5 orchestrator. Must have ``parse_failure_triage`` and
        ``hard_parse_diagnosis`` roles configured.
    db_path : Path
        Path to the SQLite database.
    role : str
        Primary role for triage (default ``"parse_failure_triage"``).
    hard_role : str
        Escalation role for low-confidence triage results
        (default ``"hard_parse_diagnosis"``).
    max_text_chars : int
        Truncate document text to this many characters (default 2000).
    """

    def __init__(
        self,
        orchestrator: OllamaOrchestrator,
        db_path: Path,
        *,
        role: str = "parse_failure_triage",
        hard_role: str = "hard_parse_diagnosis",
        max_text_chars: int = 2000,
        self_consistency_votes: int = 1,
        self_consistency_zone: tuple[float, float] = (0.5, 0.85),
    ) -> None:
        """
        Parameters
        ----------
        self_consistency_votes : int
            Total number of triage calls to make per diagnosis (1 = no voting).
            When > 1, the diagnoser keeps the majority ``failure_type`` if
            agreement >= ceil(N/2); otherwise it falls back to the hard role
            even if the first call's confidence is high.
        self_consistency_zone : (float, float)
            Confidence range that triggers extra votes. Below the lower bound
            the existing hard-role escalation handles it; above the upper bound
            the result is trusted without re-voting.
        """
        self._orch = orchestrator
        self._db_path = db_path
        self._role = role
        self._hard_role = hard_role
        self._max_text_chars = max_text_chars
        self._sc_votes = max(1, int(self_consistency_votes))
        self._sc_zone = self_consistency_zone

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select_candidates(
        self,
        *,
        limit: int = 25,
        profile: str | None = None,
        family: str | None = None,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query ``parse_attempt_logs`` for weak/empty candidates.

        Returns rows with keys: parse_attempt_id, source_pdf, family_key,
        parser_profile, effective_date, charge_count, status, confidence,
        historical_document_id, metadata_json.
        """
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Build CTE to get latest parse attempt per source + page range
            params: list[Any] = []
            cte_where = ""
            outer_where = ""
            if profile:
                cte_where += " AND pal.parser_profile = ?"
                params.append(profile)
            if since:
                cte_where += " AND pal.created_at >= ?"
                params.append(since)
            if family:
                outer_where += " AND lr.family_key = ?"
                params.append(family)

            # Exclude already-diagnosed attempts
            rows = conn.execute(
                f"""
                WITH latest_attempt AS (
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
                      AND pal.charge_count <= 5
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
                    WHERE hpr.outcome_quality = 'weak'
                       OR hpr.charge_count <= 5
                )
                SELECT la.id AS parse_attempt_id,
                       la.source_pdf,
                       la.parser_profile,
                       la.effective_date,
                       la.charge_count,
                       la.status,
                       la.confidence,
                       la.metadata_json,
                       COALESCE(NULLIF(la.parsed_historical_document_id, 0), lr.historical_document_id) AS historical_document_id,
                       COALESCE(hd.family_key, lr.family_key) AS family_key,
                       hd.raw_text_path,
                       lr.outcome_quality,
                       lr.charge_count AS run_charge_count
                FROM latest_attempt la
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
                  )
                ORDER BY la.charge_count ASC, la.confidence ASC
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

    def select_rediagnosis_candidates(
        self,
        *,
        limit: int = 25,
        profile: str | None = None,
        family: str | None = None,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """Select prior unknown/zero-confidence diagnoses for re-diagnosis.

        This is intentionally opt-in. It leaves prior diagnostic rows intact and
        appends a fresh diagnosis row after model/prompt/context improvements.
        """
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            params: list[Any] = []
            extra_where = ""
            if profile:
                extra_where += " AND pal.parser_profile = ?"
                params.append(profile)
            if family:
                extra_where += " AND COALESCE(hd.family_key, hpr.family_key) = ?"
                params.append(family)
            if since:
                extra_where += " AND ld.created_at >= ?"
                params.append(since)

            rows = conn.execute(
                f"""
                WITH latest_unknown AS (
                    SELECT ld.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY ld.parse_attempt_id
                               ORDER BY ld.id DESC
                           ) AS rn
                    FROM llm_parse_diagnostics ld
                    WHERE ld.parse_attempt_id IS NOT NULL
                      AND (ld.failure_type = 'unknown' OR COALESCE(ld.confidence, 0) = 0)
                      AND NOT EXISTS (
                          SELECT 1
                          FROM llm_parse_diagnostics newer
                          WHERE newer.parse_attempt_id = ld.parse_attempt_id
                            AND newer.id > ld.id
                            AND newer.failure_type != 'unknown'
                            AND COALESCE(newer.confidence, 0) > 0
                      )
                ),
                latest_run AS (
                    SELECT hpr.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY hpr.historical_document_id
                               ORDER BY hpr.completed_at DESC
                           ) AS rn
                    FROM historical_processing_runs hpr
                )
                SELECT pal.id AS parse_attempt_id,
                       pal.source_pdf,
                       pal.parser_profile,
                       pal.effective_date,
                       pal.charge_count,
                       pal.status,
                       pal.confidence,
                       pal.metadata_json,
                       COALESCE(
                           NULLIF(CAST(COALESCE(json_extract(pal.metadata_json, '$.historical_document_id'), '0') AS INTEGER), 0),
                           hpr.historical_document_id
                       ) AS historical_document_id,
                       COALESCE(hd.family_key, hpr.family_key) AS family_key,
                       hd.raw_text_path,
                       hpr.outcome_quality,
                       hpr.charge_count AS run_charge_count,
                       lu.id AS prior_diagnosis_id
                FROM latest_unknown lu
                JOIN parse_attempt_logs pal ON pal.id = lu.parse_attempt_id
                LEFT JOIN latest_run hpr
                  ON hpr.rn = 1
                 AND hpr.historical_document_id = NULLIF(
                       CAST(COALESCE(json_extract(pal.metadata_json, '$.historical_document_id'), '0') AS INTEGER),
                       0
                     )
                LEFT JOIN historical_documents hd
                  ON hd.id = COALESCE(
                       NULLIF(CAST(COALESCE(json_extract(pal.metadata_json, '$.historical_document_id'), '0') AS INTEGER), 0),
                       hpr.historical_document_id
                     )
                WHERE lu.rn = 1
                  AND hd.id IS NOT NULL
                  AND (
                      EXISTS (
                          SELECT 1
                          FROM ncuc_page_artifacts pa
                          WHERE pa.source_pdf = pal.source_pdf
                            AND COALESCE(pa.text_length, LENGTH(COALESCE(pa.text_content, ''))) >= 50
                      )
                      OR COALESCE(hd.raw_text_path, '') != ''
                  )
                  {extra_where}
                ORDER BY lu.id ASC
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

    def get_document_text(
        self, source_pdf: str, historical_document_id: int | None = None
    ) -> str:
        """Read text content for a PDF from page artifacts or direct extraction."""
        # Try page artifacts first
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT pa.text_content
                FROM ncuc_page_artifacts pa
                WHERE pa.source_pdf = ?
                ORDER BY pa.page_number
                LIMIT 10
                """,
                (source_pdf,),
            ).fetchall()
            if rows:
                text = "\n".join(r["text_content"] or "" for r in rows)
                if text.strip():
                    return text[: self._max_text_chars]

            # Fall back to the extracted text file persisted on historical_documents.
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
            if row and row["raw_text_path"]:
                return _read_text_file(row["raw_text_path"], self._max_text_chars)
        except Exception:
            pass
        finally:
            conn.close()
        return ""

    def get_parser_evidence(self, metadata_json: str) -> str:
        """Extract ranked candidates and evidence from metadata_json."""
        try:
            meta = json.loads(metadata_json) if metadata_json else {}
        except (json.JSONDecodeError, TypeError):
            return "No parser evidence available."

        parts: list[str] = []
        selection = meta.get("selection", {})
        if isinstance(selection, dict):
            top = selection.get("top_candidates", [])
            if top:
                parts.append("Top-ranked parser profiles:")
                for c in top[:5]:
                    if isinstance(c, dict):
                        parts.append(
                            f"  - {c.get('name', '?')}: score={c.get('score', '?')}, "
                            f"supported={c.get('supported', '?')}"
                        )
                    elif isinstance(c, str):
                        parts.append(f"  - {c}")
            reasons = selection.get("reasons", [])
            if reasons:
                parts.append(f"Selection reasons: {reasons}")

        outcome = meta.get("outcome_quality", "")
        if outcome:
            parts.append(f"Outcome quality: {outcome}")

        return "\n".join(parts) if parts else "No parser evidence available."

    def get_family_peak_charges(self, family_key: str) -> int:
        """Get the maximum charge count seen for this family."""
        if not family_key:
            return 0
        conn = sqlite3.connect(str(self._db_path))
        try:
            row = conn.execute(
                """
                SELECT MAX(charge_count) AS peak
                FROM historical_processing_runs
                WHERE family_key = ?
                  AND outcome_quality = 'strong'
                """,
                (family_key,),
            ).fetchone()
            return int(row[0]) if row and row[0] else 0
        except Exception:
            return 0
        finally:
            conn.close()

    def get_text_quality(self, source_pdf: str) -> str:
        """Get OCR/text quality summary for the document."""
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT text_quality_json
                FROM ncuc_page_artifacts
                WHERE source_pdf = ?
                  AND text_quality_json IS NOT NULL
                LIMIT 1
                """,
                (source_pdf,),
            ).fetchone()
            if row and row["text_quality_json"]:
                return row["text_quality_json"]
        except Exception:
            pass
        finally:
            conn.close()
        return "No text quality data available."

    def diagnose(self, candidate: dict[str, Any]) -> ParseFailureDiagnosis:
        """Run LLM diagnosis on one candidate. Never raises."""
        parse_attempt_id = candidate.get("parse_attempt_id", 0)
        source_pdf = candidate.get("source_pdf", "")
        family_key = candidate.get("family_key") or "unknown"
        parser_profile = candidate.get("parser_profile") or "unknown"
        effective_date = candidate.get("effective_date") or "unknown"
        charge_count = candidate.get("charge_count", 0)
        status = candidate.get("status", "unknown")
        confidence = candidate.get("confidence", 0.0)
        metadata_json = candidate.get("metadata_json", "{}")

        # Gather context
        text = self.get_document_text(
            source_pdf, candidate.get("historical_document_id")
        )
        parser_evidence = self.get_parser_evidence(metadata_json)
        expected = self.get_family_peak_charges(family_key)
        text_quality = self.get_text_quality(source_pdf)

        prompt = _DIAGNOSIS_SYSTEM_PROMPT.format(
            document_path=source_pdf,
            family_key=family_key,
            parser_profile=parser_profile,
            effective_date=effective_date,
            parser_status=status,
            parser_confidence=f"{confidence:.3f}",
            charge_count=charge_count,
            expected_charges=f"{expected} (peak for family)" if expected else "unknown",
            parser_evidence=parser_evidence,
            document_text=text if text else "(no text available)",
            text_quality=str(text_quality)[:500],
            allowed_failure_types=", ".join(ALLOWED_FAILURE_TYPES),
            allowed_actions=", ".join(ALLOWED_RECOMMENDED_ACTIONS),
        )

        # Primary triage call
        result = self._call_and_validate(
            role=self._role,
            prompt=prompt,
            parse_attempt_id=parse_attempt_id,
        )

        # Self-consistency — only when enabled AND the first call's confidence
        # falls in the uncertain zone. Below the zone the hard escalation will
        # fire; above it, a single trusted call is enough.
        zone_low, zone_high = self._sc_zone
        if (
            self._sc_votes > 1
            and zone_low <= result.confidence < zone_high
            and result.failure_type != "unknown"
        ):
            extra_results = [
                self._call_and_validate(
                    role=self._role,
                    prompt=prompt,
                    parse_attempt_id=parse_attempt_id,
                )
                for _ in range(self._sc_votes - 1)
            ]
            all_results = [result, *extra_results]
            ft_counts: dict[str, int] = {}
            for r in all_results:
                ft_counts[r.failure_type] = ft_counts.get(r.failure_type, 0) + 1
            top_ft, top_count = max(ft_counts.items(), key=lambda kv: kv[1])
            quorum = (self._sc_votes // 2) + 1  # majority threshold
            if top_count >= quorum and top_ft == result.failure_type:
                # Keep the original result, average confidence across agreers.
                agreers = [r for r in all_results if r.failure_type == top_ft]
                avg_conf = sum(r.confidence for r in agreers) / len(agreers)
                result.confidence = round(avg_conf, 3)
                result.notes = (
                    f"[self-consistency: {top_count}/{self._sc_votes} agree on {top_ft}] "
                    + result.notes
                )
            elif top_count >= quorum and top_ft != result.failure_type:
                # The majority disagreed with the first call — adopt the majority view.
                winner = next(r for r in all_results if r.failure_type == top_ft)
                winner.notes = (
                    f"[self-consistency overturned first call: {top_count}/{self._sc_votes} agree on {top_ft}] "
                    + winner.notes
                )
                result = winner
            else:
                # No majority — escalate to hard role even though first-call
                # confidence was above the existing escalation threshold.
                if self._hard_role != self._role:
                    hard_result = self._call_and_validate(
                        role=self._hard_role,
                        prompt=prompt,
                        parse_attempt_id=parse_attempt_id,
                    )
                    if hard_result.failure_type != "unknown":
                        hard_result.notes = (
                            f"[escalated: self-consistency disagreed across "
                            f"{self._sc_votes} votes] " + hard_result.notes
                        )
                        result = hard_result
                    else:
                        result.notes = (
                            f"[ambiguous: {self._sc_votes}-vote disagreement, hard role also unclear] "
                            + result.notes
                        )
                        # Demote confidence to reflect the disagreement.
                        result.confidence = min(result.confidence, 0.4)
                else:
                    result.notes = (
                        f"[ambiguous: {self._sc_votes}-vote disagreement] " + result.notes
                    )
                    result.confidence = min(result.confidence, 0.4)

        # Escalate low-confidence triage to hard role
        if (
            result.confidence < 0.5
            and result.failure_type != "unknown"
            and self._hard_role != self._role
        ):
            hard_result = self._call_and_validate(
                role=self._hard_role,
                prompt=prompt,
                parse_attempt_id=parse_attempt_id,
            )
            if hard_result.confidence >= result.confidence:
                result = hard_result
                result.notes = f"[escalated to {self._hard_role}] " + result.notes

        # Persist
        self._persist_diagnosis(result, parse_attempt_id, candidate)
        return result

    def diagnose_batch(
        self, candidates: list[dict[str, Any]], limit: int = 25
    ) -> list[ParseFailureDiagnosis]:
        """Run diagnosis on a batch of candidates. Never raises per-item."""
        results: list[ParseFailureDiagnosis] = []
        for i, candidate in enumerate(candidates[:limit]):
            try:
                result = self.diagnose(candidate)
                results.append(result)
            except Exception:
                # Continue on individual failures — don't lose the batch
                results.append(
                    ParseFailureDiagnosis(
                        failure_type="unknown",
                        confidence=0.0,
                        notes=f"Diagnoser exception for parse_attempt {candidate.get('parse_attempt_id', '?')}",
                    )
                )
        return results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _call_and_validate(
        self, *, role: str, prompt: str, parse_attempt_id: int
    ) -> ParseFailureDiagnosis:
        """Call orchestrator, validate, return diagnosis. Never raises."""
        try:
            run_result = self._orch.generate_json(
                role=role,
                prompt=prompt,
                schema=ParseFailureDiagnosis,
                subject_kind="parse_attempt",
                subject_id=str(parse_attempt_id),
                stage="parse_diagnosis",
            )
        except Exception:
            return ParseFailureDiagnosis(
                failure_type="unknown",
                confidence=0.0,
                notes="Orchestrator exception during diagnosis call",
            )

        model = run_result.model or "unknown"

        if run_result.status not in ("ok", "fallback_used"):
            return ParseFailureDiagnosis(
                failure_type="unknown",
                confidence=0.0,
                notes=f"LLM call failed: {run_result.status} — {run_result.validation_error or ''}",
            )

        diagnosis: ParseFailureDiagnosis = run_result.result

        # Validate enumerations
        if diagnosis.failure_type not in ALLOWED_FAILURE_TYPES:
            diagnosis.failure_type = "unknown"
        if diagnosis.recommended_action not in ALLOWED_RECOMMENDED_ACTIONS:
            diagnosis.recommended_action = "human_review"

        return diagnosis

    def _persist_diagnosis(
        self,
        diagnosis: ParseFailureDiagnosis,
        parse_attempt_id: int,
        candidate: dict[str, Any],
    ) -> None:
        """Write diagnosis to llm_parse_diagnostics. Best-effort."""
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute(
                """
                INSERT INTO llm_parse_diagnostics
                    (parse_attempt_id, subject_kind, subject_id, failure_type,
                     confidence, evidence_json, recommended_action, model,
                     model_role, prompt_version, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    parse_attempt_id,
                    "parse_attempt",
                    str(parse_attempt_id),
                    diagnosis.failure_type,
                    diagnosis.confidence,
                    json.dumps([e.model_dump() for e in diagnosis.evidence]),
                    diagnosis.recommended_action,
                    (self._orch.roles.get(self._role) or type("x", (), {"primary": "unknown"})()).primary,
                    self._role,
                    "v1",
                    diagnosis.notes[:2000] if diagnosis.notes else "",
                ),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass  # best-effort persistence


def _read_text_file(path_value: str, max_chars: int) -> str:
    """Best-effort read of an extracted text file path."""
    try:
        path = Path(path_value)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception:
        return ""
