"""
LLM adjudication classifier (Phase 5).

When rule-based and embedding classifiers disagree, or confidence is low,
this module calls an LLM via the Phase 2.5 orchestrator to provide a
structured second opinion.

The LLM is shown the document text, rule result, embedding result, and the
candidate taxonomy labels. It returns a strict JSON verdict that is validated
and converted to a ``ClassificationResult``.

LLM evidence is recorded but is NOT load-bearing without human confirmation
(Phase 6). The label only "counts" once confirmed via review.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from duke_rates.classification.result import ClassificationResult


# ---------------------------------------------------------------------------
# LLM response schema
# ---------------------------------------------------------------------------


class LLMAdjudicationVerdict(BaseModel):
    """Structured output the LLM must produce.

    Accepts common LLM field-name variations (reasoning/rationale,
    document_type/label/verdict) since different models default to
    different conventions.
    """

    document_type: str = Field(default="UNKNOWN", description="Best document type from the allowed taxonomy")
    label: str = Field(default="", description="Alias for document_type")
    verdict: str = Field(default="", description="Alias for document_type")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Confidence in the chosen label")
    reasoning: str = Field(default="", description="Brief explanation of why this label was chosen")
    rationale: str = Field(default="", description="Alias for reasoning")
    key_signals: list[str] = Field(default_factory=list, description="Text signals that drove the decision")

    @model_validator(mode="after")
    def _normalize_fields(self) -> "LLMAdjudicationVerdict":
        # Normalize document_type
        if self.document_type == "UNKNOWN" and self.label:
            self.document_type = self.label
        if self.document_type == "UNKNOWN" and self.verdict:
            self.document_type = self.verdict
        # Normalize reasoning
        if not self.reasoning and self.rationale:
            self.reasoning = self.rationale
        if not self.reasoning:
            self.reasoning = "No reasoning provided"
        return self


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

_ADJUDICATION_SYSTEM_PROMPT = """\
You are a regulatory document classifier for the North Carolina Utilities Commission (NCUC).
Your task is to classify a docket document into exactly ONE document_type from the taxonomy below.

## Allowed document_type labels:
{taxonomy_list}

## Rules:
1. Choose the SINGLE best label from the allowed list above.
2. If no label clearly fits, respond with document_type="UNKNOWN" and low confidence.
3. Base your decision on the document TEXT, not on the prior classifier results.
4. The prior results (rule-based and embedding) are suggestions only — override them if the text supports a different label.
5. Provide 2-5 specific text signals that justify your choice in key_signals.
6. Confidence must be 0.0-1.0. Use 0.7+ only when the text is unambiguous.

## Document type descriptions:
{taxonomy_descriptions}

## Prior classifier results:
- Rule-based classifier says: {rule_label} (confidence: {rule_confidence})
- Embedding similarity classifier says: {embedding_label} (confidence: {embedding_confidence})

## Document text (first ~2000 characters):
```
{document_text}
```

Respond with a single JSON object matching this exact schema. No other text."""


# ---------------------------------------------------------------------------
# Adjudicator
# ---------------------------------------------------------------------------


class LLMAdjudicator:
    """LLM-based second opinion for document_type classification.

    Parameters
    ----------
    orchestrator : OllamaOrchestrator
        Phase 2.5 orchestrator with ``balanced_classifier`` role.
    db_path : Path
        Path to the SQLite database (for reading taxonomy).
    role : str
        Orchestrator role for adjudication (default ``"balanced_classifier"``).
    max_text_chars : int
        Truncate document text to this many characters (default 2500).
    """

    def __init__(
        self,
        orchestrator: Any,
        db_path: Path,
        *,
        role: str = "balanced_classifier",
        max_text_chars: int = 2500,
    ) -> None:
        self._orchestrator = orchestrator
        self._db_path = db_path
        self._role = role
        self._max_text_chars = max_text_chars
        self._taxonomy_labels: list[str] = []
        self._taxonomy_descriptions: list[str] = []
        self._load_taxonomy()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def adjudicate(
        self,
        text: str,
        *,
        rule_result: ClassificationResult | None = None,
        embedding_result: ClassificationResult | None = None,
    ) -> ClassificationResult:
        """Run LLM adjudication and return a ``ClassificationResult``.

        Never raises — returns ``UNKNOWN`` with confidence 0.0 on any failure.
        """
        if not text or not text.strip():
            return self._unknown_result("no_text")

        if not self._taxonomy_labels:
            return self._unknown_result("no_taxonomy")

        # Check health
        ok, err = self._orchestrator.health_probe(self._role)
        if not ok:
            return self._unknown_result("model_unavailable", error=err)

        # Build prompt
        prompt = self._build_prompt(text, rule_result, embedding_result)

        try:
            run_result = self._orchestrator.generate_json(
                role=self._role,
                prompt=prompt,
                schema=LLMAdjudicationVerdict,
                subject_kind="adhoc",
                subject_id="0",
                stage="document_type",
            )
        except Exception:
            return self._unknown_result("orchestrator_error")

        model = run_result.model or "unknown"

        if run_result.status not in ("ok", "fallback_used"):
            # Try to salvage a label from the raw payload
            label = self._extract_label_from_raw(run_result.raw_payload or "")
            return ClassificationResult(
                label=label,
                confidence=0.0 if label == "UNKNOWN" else 0.3,
                classifier=f"llm_{model}_v1",
                classifier_version="v1",
                evidence=[
                    {"kind": f"llm_{run_result.status}", "value": 0,
                     "error": run_result.validation_error},
                ],
                metadata={
                    "model": model,
                    "role": self._role,
                    "prompt_version": "v1",
                },
            )

        verdict: LLMAdjudicationVerdict = run_result.result

        # Validate label against taxonomy
        label = verdict.document_type.strip() if verdict.document_type else "UNKNOWN"
        if label not in self._taxonomy_labels and label != "UNKNOWN":
            label = self._extract_label_from_raw(run_result.raw_payload or "")
            if label not in self._taxonomy_labels and label != "UNKNOWN":
                label = "UNKNOWN"
                verdict.confidence = 0.0

        return ClassificationResult(
            label=label,
            confidence=round(float(verdict.confidence), 4),
            classifier=f"llm_{model}_v1",
            classifier_version="v1",
            evidence=[
                {"kind": "llm_reasoning", "value": verdict.reasoning, "weight": 1.0},
                {"kind": "key_signals", "value": verdict.key_signals, "weight": 0.8},
                {"kind": "rule_input", "value": rule_result.label if rule_result else "N/A"},
                {"kind": "embedding_input", "value": embedding_result.label if embedding_result else "N/A"},
                {"kind": "orchestrator_status", "value": run_result.status},
            ],
            alternatives=[],
            metadata={
                "model": model,
                "role": self._role,
                "tokens_in": run_result.tokens_in,
                "tokens_out": run_result.tokens_out,
                "duration_ms": run_result.duration_ms,
                "prompt_version": "v1",
            },
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_taxonomy(self) -> None:
        """Load terminal document types from the taxonomy table."""
        import sqlite3

        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT code, description
                    FROM document_types
                    WHERE is_terminal = 1
                    ORDER BY primary_category, code
                    """
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            return

        self._taxonomy_labels = [r["code"] for r in rows]
        self._taxonomy_descriptions = [
            f"  {r['code']}: {r['description'] or 'No description'}"
            for r in rows
        ]

    def _build_prompt(
        self,
        text: str,
        rule_result: ClassificationResult | None,
        embedding_result: ClassificationResult | None,
    ) -> str:
        truncated = text[: self._max_text_chars].strip()

        return _ADJUDICATION_SYSTEM_PROMPT.format(
            taxonomy_list=", ".join(self._taxonomy_labels),
            taxonomy_descriptions="\n".join(self._taxonomy_descriptions),
            rule_label=rule_result.label if rule_result else "N/A",
            rule_confidence=f"{rule_result.confidence:.3f}" if rule_result else "N/A",
            embedding_label=embedding_result.label if embedding_result else "N/A",
            embedding_confidence=f"{embedding_result.confidence:.3f}" if embedding_result else "N/A",
            document_text=truncated,
        )

    def _extract_label_from_raw(self, raw: str) -> str:
        """Attempt to extract a document_type label from a non-conforming JSON payload."""
        import json as _json
        try:
            data = _json.loads(raw)
        except Exception:
            return "UNKNOWN"
        for key in ("document_type", "label", "verdict", "type", "category"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                candidate = val.strip()
                if candidate in self._taxonomy_labels:
                    return candidate
        return "UNKNOWN"

    def _unknown_result(self, reason: str, **extra: Any) -> ClassificationResult:
        return ClassificationResult(
            label="UNKNOWN",
            confidence=0.0,
            classifier="llm_unknown_v1",
            classifier_version="v1",
            evidence=[{"kind": reason, "value": 0, **(extra or {})}],
        )
