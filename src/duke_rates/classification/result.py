"""ClassificationResult — canonical type for any classifier in the pipeline.

A classifier is anything that picks a label for a subject (a document type,
a family key, a parser profile, an OCR backend, etc.). Today these decisions
are scattered across rule branches and metadata blobs; this type makes them
uniform so they can be persisted, audited, and overlaid.

Design notes:
- ``confidence`` is bounded 0..1 by classifier convention; raw scores from
  rule-based scorers should be normalized at the call site.
- ``alternatives`` carries runner-up labels with their scores so disagreement
  reports can surface low-margin decisions where rules and second-opinion
  classifiers might disagree.
- ``evidence`` is loosely structured — each entry is ``{kind, value, weight}``
  where the classifier picks meaningful kinds. The free-form shape lets new
  classifiers add new evidence types without schema changes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ClassificationResult(BaseModel):
    """One classifier's output for one subject."""

    label: str
    confidence: float = Field(ge=0.0, le=1.0)
    classifier: str
    classifier_version: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    alternatives: list[tuple[str, float]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_score_breakdown(
        cls,
        *,
        label: str,
        score: float,
        score_breakdown: dict[str, float] | None,
        all_scores: dict[str, float] | None,
        classifier: str,
        classifier_version: str = "",
        score_to_confidence: float | None = None,
    ) -> "ClassificationResult":
        """Build a result from a rule-scorer's raw output.

        ``score_breakdown`` maps evidence kinds to their contribution
        (e.g. ``{"explicit_leaf_hit": 40.0, "schedule_code_hit": 20.0}``).
        ``all_scores`` is the score for every candidate label, used to derive
        runner-up alternatives.
        ``score_to_confidence`` is the divisor used to normalize ``score``
        into 0..1 (e.g. the maximum theoretically achievable score). When
        omitted, falls back to clamp(score / max(all_scores or [score], 1)).
        """
        if score_to_confidence and score_to_confidence > 0:
            confidence = max(0.0, min(1.0, score / score_to_confidence))
        else:
            denom = max([s for s in (all_scores or {}).values()] + [score, 1.0])
            confidence = max(0.0, min(1.0, score / denom))

        evidence = []
        for kind, weight in (score_breakdown or {}).items():
            evidence.append({"kind": kind, "weight": float(weight)})

        alternatives: list[tuple[str, float]] = []
        if all_scores:
            ranked = sorted(all_scores.items(), key=lambda kv: kv[1], reverse=True)
            for alt_label, alt_score in ranked:
                if alt_label == label:
                    continue
                alternatives.append((alt_label, float(alt_score)))
            alternatives = alternatives[:5]  # cap

        return cls(
            label=label,
            confidence=confidence,
            classifier=classifier,
            classifier_version=classifier_version,
            evidence=evidence,
            alternatives=alternatives,
        )
