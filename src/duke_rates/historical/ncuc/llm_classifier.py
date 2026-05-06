"""
Stage 10: Optional LLM-assisted classification.

Used ONLY after local scoring and family grouping have already narrowed the
candidate set substantially.  This module is optional and modular — it
gracefully degrades if no API key is configured.

Classifies narrowed candidates using a structured prompt, extracting:
- Document type (tariff_sheet, rider, rate_schedule, order, testimony, exhibit, other)
- Finality estimate (final, intermediate_revision, redline, procedural, unknown)
- Utility name
- Rider/schedule name
- Effective date
- Confidence
- Short rationale

The LLM result augments (does not replace) the deterministic local scoring.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from duke_rates.historical.ncuc.result_scorer import ScoredResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Classification schema
# ---------------------------------------------------------------------------

@dataclass
class LLMClassification:
    """Structured LLM classification output for a single document candidate."""
    doc_type: str               # tariff_sheet | rider | rate_schedule | order |
                                # testimony | exhibit | draft | redline | unrelated | other
    likely_finality: str        # final | intermediate_revision | redline | procedural | unknown
    utility: str | None
    rider_name: str | None
    schedule_name: str | None
    effective_date: str | None
    revision_status: str | None # clean | draft | redlined | unknown
    confidence: float           # 0.0–1.0
    rationale: str
    raw_response: str = ""
    model_used: str = ""
    error: str | None = None


_CLASSIFICATION_PROMPT = """You are a regulatory document classifier specializing in electric utility tariff filings.

Analyze the following document metadata and classify it.

DOCUMENT METADATA:
Title: {title}
URL: {url}
Snippet: {snippet}
Docket: {docket}
Filing date: {filing_date}
Extracted schedule codes: {schedule_codes}
Extracted rider codes: {rider_codes}

Your task: classify this as precisely as possible.

Return ONLY valid JSON with these exact fields:
{{
  "doc_type": "tariff_sheet|rider|rate_schedule|order|testimony|exhibit|draft|redline|unrelated|other",
  "likely_finality": "final|intermediate_revision|redline|procedural|unknown",
  "utility": "<utility name or null>",
  "rider_name": "<rider name or null>",
  "schedule_name": "<schedule name/number or null>",
  "effective_date": "<YYYY-MM-DD or null>",
  "revision_status": "clean|draft|redlined|unknown",
  "confidence": <0.0-1.0>,
  "rationale": "<1-2 sentence explanation>"
}}

Classification guidance:
- tariff_sheet: an official published tariff page with rate language (most ideal)
- rider: a tariff rider or adjustment clause with rate amounts
- rate_schedule: a rate schedule with customer service conditions and charges
- order: a commission order approving or modifying rates
- testimony: witness testimony (not ideal)
- exhibit: an exhibit to testimony or filing (not ideal)
- draft: a draft or proposed version not yet approved (not ideal)
- redline: a redline/markup comparison of changes (not ideal)
- unrelated: clearly unrelated to Duke Energy tariffs

For Duke Energy Progress (NC), known docket series: E-2 and sub-dockets.
For Duke Energy Carolinas (NC), known docket series: E-7 and sub-dockets.
"""


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------

class LLMClassifier:
    """
    Optional LLM-based structured classification.
    Wraps the OpenAI API (or any compatible endpoint).
    Degrades gracefully if no API key is present.
    """

    def __init__(self, settings, model: str | None = None):
        self.settings = settings
        self.model = model or getattr(settings, "openai_model", "gpt-4.1-mini")
        self._client = None
        self._available = False
        self._init_client()

    def _init_client(self) -> None:
        api_key = getattr(self.settings, "openai_api_key", None)
        if not api_key:
            logger.info("LLMClassifier: no OpenAI API key configured — LLM classification disabled")
            return
        try:
            import openai
            self._client = openai.OpenAI(api_key=api_key)
            self._available = True
            logger.info("LLMClassifier initialized with model %s", self.model)
        except ImportError:
            logger.warning("LLMClassifier: openai package not installed — run: pip install openai")
        except Exception as exc:
            logger.warning("LLMClassifier: failed to initialize: %s", exc)

    @property
    def is_available(self) -> bool:
        return self._available

    def classify_one(self, scored: "ScoredResult") -> LLMClassification:
        """Classify a single ScoredResult. Returns an LLMClassification."""
        if not self._available:
            return LLMClassification(
                doc_type="unknown",
                likely_finality="unknown",
                utility=None,
                rider_name=None,
                schedule_name=None,
                effective_date=None,
                revision_status="unknown",
                confidence=0.0,
                rationale="LLM not available",
                error="llm_not_available",
            )

        result = scored.result
        prompt = _CLASSIFICATION_PROMPT.format(
            title=result.title or "(no title)",
            url=result.url or "",
            snippet=(result.snippet or "")[:500],
            docket=f"{result.docket_number or ''} Sub {result.sub_number or ''}".strip(),
            filing_date=result.filing_date or "(unknown)",
            schedule_codes=", ".join(result.extracted_schedule_codes) or "(none)",
            rider_codes=", ".join(result.extracted_rider_codes) or "(none)",
        )

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a regulatory document classifier. "
                            "Always respond with valid JSON only, no markdown."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=400,
            )
            raw = response.choices[0].message.content or ""
            return self._parse_response(raw, model=self.model)
        except Exception as exc:
            logger.warning("LLM classification failed: %s", exc)
            return LLMClassification(
                doc_type="unknown",
                likely_finality="unknown",
                utility=None,
                rider_name=None,
                schedule_name=None,
                effective_date=None,
                revision_status="unknown",
                confidence=0.0,
                rationale="LLM API error",
                error=str(exc)[:200],
                model_used=self.model,
            )

    def classify_batch(
        self,
        scored_list: list["ScoredResult"],
        *,
        max_candidates: int = 20,
    ) -> list[tuple["ScoredResult", LLMClassification]]:
        """
        Classify a list of candidates (up to max_candidates).
        Returns list of (ScoredResult, LLMClassification) pairs.
        Only processes top candidates by combined_score.
        """
        candidates = sorted(scored_list, key=lambda s: -s.combined_score)[:max_candidates]
        results = []
        for scored in candidates:
            classification = self.classify_one(scored)
            results.append((scored, classification))
        return results

    @staticmethod
    def _parse_response(raw: str, model: str = "") -> LLMClassification:
        """Parse JSON from the LLM response."""
        # Strip markdown code fences if present
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if len(lines) > 2 else text

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return LLMClassification(
                doc_type="unknown",
                likely_finality="unknown",
                utility=None,
                rider_name=None,
                schedule_name=None,
                effective_date=None,
                revision_status="unknown",
                confidence=0.0,
                rationale="JSON parse failed",
                raw_response=raw[:500],
                model_used=model,
                error=str(exc),
            )

        return LLMClassification(
            doc_type=data.get("doc_type", "other"),
            likely_finality=data.get("likely_finality", "unknown"),
            utility=data.get("utility"),
            rider_name=data.get("rider_name"),
            schedule_name=data.get("schedule_name"),
            effective_date=data.get("effective_date"),
            revision_status=data.get("revision_status", "unknown"),
            confidence=float(data.get("confidence", 0.0)),
            rationale=data.get("rationale", ""),
            raw_response=raw[:500],
            model_used=model,
        )
