"""RAG eval harness — measure retrieval + generation quality against a held-out set.

Loads ``tests/rag_eval_set.yaml`` (or a custom path), runs each case
through ``RagRetriever`` (and optionally ``RagGenerator``), scores the
results against the expected matchers, and emits aggregate metrics.

This is what lets us answer "did this change help the RAG?" with a number
instead of a hunch. Run with ``--full`` to include LLM generation
(slower; ~1 min/case); without ``--full`` only retrieval is scored
(seconds per case).

Metrics:
  retrieval:
    recall_at_5 — frac of cases where the expected source appears in top-5
    recall_at_10 — same for top-10
    mrr_at_10 — mean reciprocal rank, 0 if expected source not in top-10
    avg_top1_similarity — average cosine similarity of the rank-1 hit
  generation (only with --full):
    grounded_rate — frac of answered cases that contained at least one [N] citation
    keyword_match_rate — frac with expected keywords in the answer
    correct_refusal_rate — frac of expected_no_answer cases that refused
    false_answer_rate — frac of expected_no_answer cases that DID answer
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from duke_rates.document_intelligence.rag_generator import RagAnswer, RagGenerator
from duke_rates.document_intelligence.rag_retriever import (
    RagRetriever,
    RetrievalHit,
)


_DEFAULT_REFUSAL_TEXT = "i could not find this in the indexed documents"


@dataclass(frozen=True)
class EvalCase:
    """One entry in the eval set."""

    id: str
    question: str
    section_types: list[str] = field(default_factory=list)
    expected_source_pdf_substrings: list[str] = field(default_factory=list)
    expected_leaf_numbers: list[str] = field(default_factory=list)
    expected_schedule_codes: list[str] = field(default_factory=list)
    expected_answer_keywords: list[str] = field(default_factory=list)
    expected_no_answer: bool = False
    notes: str = ""

    @property
    def has_retrieval_target(self) -> bool:
        return bool(
            self.expected_source_pdf_substrings
            or self.expected_leaf_numbers
            or self.expected_schedule_codes
        )


@dataclass
class CaseRetrievalResult:
    case_id: str
    hits_returned: int
    top1_similarity: float | None
    expected_rank: int | None  # 1-based rank of first expected hit; None if not found
    matched_via: str | None  # 'leaf', 'schedule', 'source_pdf', or None
    section_type_filter: list[str] | None


@dataclass
class CaseGenerationResult:
    case_id: str
    answered: bool  # False iff the LLM refused
    grounded: bool
    cited_count: int
    keyword_matches: list[str]
    answer_text: str
    expected_refusal_correct: bool | None  # only meaningful when expected_no_answer
    llm_status: str
    generation_ms: float


@dataclass
class EvalReport:
    cases: list[EvalCase]
    retrieval_results: list[CaseRetrievalResult]
    generation_results: list[CaseGenerationResult] = field(default_factory=list)

    def retrieval_metrics(self) -> dict[str, Any]:
        # Only cases with a real retrieval target contribute.
        in_scope = [
            (c, r) for c, r in zip(self.cases, self.retrieval_results)
            if c.has_retrieval_target
        ]
        n = len(in_scope)
        if n == 0:
            return {
                "n_cases": 0,
                "recall_at_5": None,
                "recall_at_10": None,
                "mrr_at_10": None,
                "avg_top1_similarity": None,
            }
        recall_5 = sum(
            1 for _, r in in_scope if r.expected_rank and r.expected_rank <= 5
        ) / n
        recall_10 = sum(
            1 for _, r in in_scope if r.expected_rank and r.expected_rank <= 10
        ) / n
        mrr = sum(
            (1.0 / r.expected_rank) if r.expected_rank and r.expected_rank <= 10 else 0.0
            for _, r in in_scope
        ) / n
        sims = [r.top1_similarity for _, r in in_scope if r.top1_similarity is not None]
        avg_sim = sum(sims) / len(sims) if sims else None
        return {
            "n_cases": n,
            "recall_at_5": round(recall_5, 3),
            "recall_at_10": round(recall_10, 3),
            "mrr_at_10": round(mrr, 3),
            "avg_top1_similarity": round(avg_sim, 4) if avg_sim is not None else None,
        }

    def generation_metrics(self) -> dict[str, Any]:
        if not self.generation_results:
            return {"n_cases": 0}

        by_id = {g.case_id: g for g in self.generation_results}
        n_total = len(self.cases)

        # Answered (non-refused) cases
        answered = [
            (c, by_id[c.id])
            for c in self.cases
            if c.id in by_id and by_id[c.id].answered
        ]
        # Refusal-expected cases
        refusal_expected = [c for c in self.cases if c.expected_no_answer]
        # Answer-expected cases (have retrieval target, not refusal-expected)
        answer_expected = [
            c for c in self.cases
            if not c.expected_no_answer and c.has_retrieval_target
        ]

        grounded_rate = (
            sum(1 for _, g in answered if g.grounded) / len(answered)
            if answered else None
        )

        # Keyword-match rate: fraction of answer-expected cases where at
        # least one expected_answer_keyword appears in the (lowercased)
        # answer text.
        keyword_match_cases = [
            c for c in answer_expected if c.expected_answer_keywords
        ]
        keyword_match_rate = (
            sum(
                1 for c in keyword_match_cases
                if c.id in by_id and by_id[c.id].keyword_matches
            ) / len(keyword_match_cases)
            if keyword_match_cases else None
        )

        # Correct refusal: expected_no_answer AND model refused
        correct_refusal_rate = (
            sum(
                1 for c in refusal_expected
                if c.id in by_id and by_id[c.id].expected_refusal_correct
            ) / len(refusal_expected)
            if refusal_expected else None
        )

        return {
            "n_cases": n_total,
            "n_answered": len(answered),
            "n_refusal_expected": len(refusal_expected),
            "grounded_rate": round(grounded_rate, 3) if grounded_rate is not None else None,
            "keyword_match_rate": round(keyword_match_rate, 3) if keyword_match_rate is not None else None,
            "correct_refusal_rate": round(correct_refusal_rate, 3) if correct_refusal_rate is not None else None,
        }


# ----------------------------------------------------------------------
# Loaders + scoring
# ----------------------------------------------------------------------


def load_eval_set(path: Path) -> list[EvalCase]:
    """Parse the YAML eval set into EvalCase objects."""
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or "cases" not in data:
        raise ValueError(f"{path} missing 'cases' key")
    cases: list[EvalCase] = []
    for raw in data["cases"]:
        cases.append(
            EvalCase(
                id=str(raw["id"]),
                question=str(raw["question"]),
                section_types=list(raw.get("section_types") or []),
                expected_source_pdf_substrings=list(
                    raw.get("expected_source_pdf_substrings") or []
                ),
                expected_leaf_numbers=[
                    str(x) for x in (raw.get("expected_leaf_numbers") or [])
                ],
                expected_schedule_codes=[
                    str(x) for x in (raw.get("expected_schedule_codes") or [])
                ],
                expected_answer_keywords=list(
                    raw.get("expected_answer_keywords") or []
                ),
                expected_no_answer=bool(raw.get("expected_no_answer", False)),
                notes=str(raw.get("notes", "")),
            )
        )
    return cases


def score_retrieval(case: EvalCase, hits: list[RetrievalHit]) -> CaseRetrievalResult:
    """Find the rank of the first hit that matches any expected_* criterion."""
    top1_sim = hits[0].similarity if hits else None
    if not case.has_retrieval_target:
        return CaseRetrievalResult(
            case_id=case.id,
            hits_returned=len(hits),
            top1_similarity=top1_sim,
            expected_rank=None,
            matched_via=None,
            section_type_filter=list(case.section_types) or None,
        )

    sched_set = {c.upper() for c in case.expected_schedule_codes}
    leaf_set = {str(x) for x in case.expected_leaf_numbers}
    pdf_subs = [s.lower() for s in case.expected_source_pdf_substrings]

    for rank, hit in enumerate(hits, 1):
        # leaf number match
        if leaf_set and any(str(ln) in leaf_set for ln in hit.leaf_numbers):
            return CaseRetrievalResult(
                case_id=case.id,
                hits_returned=len(hits),
                top1_similarity=top1_sim,
                expected_rank=rank,
                matched_via="leaf",
                section_type_filter=list(case.section_types) or None,
            )
        # schedule code match (uppercase)
        if sched_set and any(c.upper() in sched_set for c in hit.schedule_codes):
            return CaseRetrievalResult(
                case_id=case.id,
                hits_returned=len(hits),
                top1_similarity=top1_sim,
                expected_rank=rank,
                matched_via="schedule",
                section_type_filter=list(case.section_types) or None,
            )
        # source_pdf substring match
        pdf_lower = hit.source_pdf.lower()
        if pdf_subs and any(s in pdf_lower for s in pdf_subs):
            return CaseRetrievalResult(
                case_id=case.id,
                hits_returned=len(hits),
                top1_similarity=top1_sim,
                expected_rank=rank,
                matched_via="source_pdf",
                section_type_filter=list(case.section_types) or None,
            )

    return CaseRetrievalResult(
        case_id=case.id,
        hits_returned=len(hits),
        top1_similarity=top1_sim,
        expected_rank=None,
        matched_via=None,
        section_type_filter=list(case.section_types) or None,
    )


def score_generation(case: EvalCase, answer: RagAnswer) -> CaseGenerationResult:
    text_lower = answer.answer.lower().strip()
    refused = _DEFAULT_REFUSAL_TEXT in text_lower

    keyword_matches: list[str] = []
    for kw in case.expected_answer_keywords:
        if kw.lower() in text_lower:
            keyword_matches.append(kw)

    expected_refusal_correct: bool | None
    if case.expected_no_answer:
        expected_refusal_correct = refused
    else:
        expected_refusal_correct = None

    return CaseGenerationResult(
        case_id=case.id,
        answered=not refused,
        grounded=answer.is_grounded,
        cited_count=len(answer.cited_indices),
        keyword_matches=keyword_matches,
        answer_text=answer.answer,
        expected_refusal_correct=expected_refusal_correct,
        llm_status=answer.llm_status,
        generation_ms=answer.generation_ms,
    )


# ----------------------------------------------------------------------
# Runners
# ----------------------------------------------------------------------


def run_retrieval_eval(
    cases: list[EvalCase],
    retriever: RagRetriever,
    *,
    top_k: int = 10,
    progress_callback: Any = None,
) -> list[CaseRetrievalResult]:
    """Run retrieval-only eval (fast)."""
    out: list[CaseRetrievalResult] = []
    for i, case in enumerate(cases):
        hits = retriever.search(
            case.question,
            top_k=top_k,
            section_types=case.section_types or None,
        )
        out.append(score_retrieval(case, hits))
        if progress_callback:
            progress_callback(i + 1, len(cases), case.id)
    return out


def run_full_eval(
    cases: list[EvalCase],
    generator: RagGenerator,
    *,
    top_k: int = 8,
    progress_callback: Any = None,
) -> tuple[list[CaseRetrievalResult], list[CaseGenerationResult]]:
    """Run retrieval + generation eval (slow — ~1 min/case)."""
    retrieval: list[CaseRetrievalResult] = []
    generation: list[CaseGenerationResult] = []
    for i, case in enumerate(cases):
        answer = generator.answer(
            case.question,
            top_k=top_k,
            section_types=case.section_types or None,
        )
        retrieval.append(score_retrieval(case, answer.hits))
        generation.append(score_generation(case, answer))
        if progress_callback:
            progress_callback(i + 1, len(cases), case.id)
    return retrieval, generation
