"""RAG generation layer (R2): wraps RagRetriever with LLM synthesis.

Pipeline:
1. RagRetriever returns top-k section hits for the user's query.
2. We assemble a numbered-context prompt with citations.
3. Call the LLM (OllamaOrchestrator generate_text, default role
   ``balanced_classifier`` → qwen3:8b).
4. Parse [N] citation markers from the answer.
5. Return RagAnswer with the answer text, parsed citations, and the
   underlying hits so callers can show grounding.

Design choices:
- We deliberately ban free generation when the answer isn't in context —
  the prompt instructs the model to respond "I could not find this..."
  rather than hallucinate. This is the most important safety property
  for a tariff-citation system.
- max_context_chars caps the assembled context to keep prompts under the
  LLM context window. Excerpts are truncated, not entire hits dropped.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from duke_rates.document_intelligence.rag_retriever import (
    RagRetriever,
    RetrievalHit,
)


# Accept several common citation formats LLMs emit:
#   [1]           — canonical
#   [N1] / [n1]   — qwen2.5:7b-instruct fallback
#   [Ref 1]       — some instruction-tuned models
# Always captures the digit. Prefix (if any) is consumed and ignored.
_CITATION_RE = re.compile(r"\[(?:N|n|Ref\s*|Source\s*)?(\d+)\]")


_SYSTEM_PROMPT = """You are an expert assistant for North Carolina utility tariff documents.
Answer the user's question using ONLY the numbered context blocks below.

Rules:
- Cite every factual claim with bracketed digits: [1], [2], [3]. Example: "The fuel adjustment was 0.262 cents per kWh [1]."
- Do NOT use [N1] or [Ref 1] — only [1], [2], etc.
- If the answer is not in the context, respond exactly: "I could not find this in the indexed documents."
- Quote specific rate values, leaf numbers, and schedule codes exactly as they appear.
- Keep your answer concise: typically 2-5 sentences.
- Do not invent rates, dates, or schedule codes that are not in the context."""


@dataclass(frozen=True)
class RagAnswer:
    """Result of one RAG query."""

    question: str
    answer: str
    hits: list[RetrievalHit]  # all hits retrieved (in rank order)
    cited_indices: list[int]  # 1-based, parsed from [N] markers in answer
    llm_model: str
    llm_status: str  # 'ok' | 'timeout' | 'http_error' | 'no_context'
    retrieval_ms: float
    generation_ms: float
    prompt: str = ""  # only kept if include_prompt=True in generator

    def cited_hits(self) -> list[tuple[int, RetrievalHit]]:
        """Return (rank, hit) pairs for citations the model actually used."""
        out: list[tuple[int, RetrievalHit]] = []
        for n in self.cited_indices:
            if 1 <= n <= len(self.hits):
                out.append((n, self.hits[n - 1]))
        return out

    def uncited_hits(self) -> list[tuple[int, RetrievalHit]]:
        cited = set(self.cited_indices)
        return [
            (i + 1, h)
            for i, h in enumerate(self.hits)
            if (i + 1) not in cited
        ]

    @property
    def is_grounded(self) -> bool:
        """True iff the LLM cited at least one context block."""
        return len(self.cited_indices) > 0


def _parse_citations(answer: str, max_n: int) -> list[int]:
    """Extract [N] markers, deduped + sorted, bounded by max_n."""
    seen: set[int] = set()
    out: list[int] = []
    for m in _CITATION_RE.finditer(answer):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        if 1 <= n <= max_n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _build_context_blocks(
    hits: list[RetrievalHit],
    *,
    max_context_chars: int,
    max_excerpt_chars: int,
) -> str:
    """Format retrieved hits into a numbered-context section.

    Truncates each excerpt to ``max_excerpt_chars`` and stops adding blocks
    when the total length crosses ``max_context_chars``. Always returns at
    least the first block, even if it exceeds the budget — pathological,
    but never an empty context.
    """
    chunks: list[str] = []
    running = 0
    for i, h in enumerate(hits, 1):
        excerpt = (h.text_excerpt or "").replace("\f", " ").strip()
        if len(excerpt) > max_excerpt_chars:
            excerpt = excerpt[:max_excerpt_chars].rstrip() + "..."
        # Metadata prefix: tells the LLM what kind of section this is
        # before it sees the (often OCR-noisy) text. Helps disambiguate
        # collaborative-meeting slides from actual rate schedules even
        # when both carry the same schedule_code.
        meta_bits: list[str] = []
        if h.section_type:
            meta_bits.append(f"section_type={h.section_type}")
        if h.schedule_codes:
            meta_bits.append(f"schedule_codes={','.join(h.schedule_codes[:5])}")
        if h.rider_codes:
            meta_bits.append(f"rider_codes={','.join(h.rider_codes[:5])}")
        if h.leaf_numbers:
            meta_bits.append(f"leaf={','.join(h.leaf_numbers[:5])}")
        meta_line = " | ".join(meta_bits) if meta_bits else ""
        block = f"[{i}] {h.citation()}  (similarity {h.similarity:.3f})"
        if meta_line:
            block += f"\n{meta_line}"
        block += f"\n{excerpt}"
        if running > 0 and running + len(block) > max_context_chars:
            break
        chunks.append(block)
        running += len(block) + 2
    return "\n\n".join(chunks)


def build_prompt(
    question: str,
    hits: list[RetrievalHit],
    *,
    max_context_chars: int = 8000,
    max_excerpt_chars: int = 800,
) -> str:
    """Assemble the full LLM prompt. Exposed for tests + debugging."""
    context = _build_context_blocks(
        hits,
        max_context_chars=max_context_chars,
        max_excerpt_chars=max_excerpt_chars,
    )
    return (
        f"{_SYSTEM_PROMPT}\n\nCONTEXT:\n{context}\n\nQUESTION: {question}\n\nANSWER:"
    )


class RagGenerator:
    """End-to-end RAG: retrieve → prompt → generate → parse citations.

    Parameters
    ----------
    retriever : RagRetriever
        Already-configured section-level retriever.
    orchestrator : OllamaOrchestrator
        For ``generate_text`` calls.
    generation_role : str
        Orchestrator role to use (default ``"balanced_classifier"`` →
        qwen3:8b).
    top_k : int
        How many hits to retrieve per query (default 8).
    max_context_chars : int
        Cap on total context block bytes in the prompt (default 8000).
    max_excerpt_chars : int
        Cap on each individual excerpt (default 800).
    include_prompt : bool
        If True, RagAnswer.prompt is populated. Defaults False to keep
        return payloads small.
    """

    def __init__(
        self,
        retriever: RagRetriever,
        orchestrator: Any,
        *,
        generation_role: str = "balanced_classifier",
        top_k: int = 8,
        max_context_chars: int = 16000,
        max_excerpt_chars: int = 2000,
        include_prompt: bool = False,
    ) -> None:
        self._retriever = retriever
        self._orchestrator = orchestrator
        self._generation_role = generation_role
        self._top_k = top_k
        self._max_context_chars = max_context_chars
        self._max_excerpt_chars = max_excerpt_chars
        self._include_prompt = include_prompt

    def answer(
        self,
        question: str,
        *,
        top_k: int | None = None,
        section_types: list[str] | None = None,
        schedule_code_like: str | None = None,
        source_pdf_like: str | None = None,
        min_similarity: float = 0.0,
    ) -> RagAnswer:
        """Run one RAG query end-to-end."""
        if not question or not question.strip():
            return RagAnswer(
                question=question,
                answer="",
                hits=[],
                cited_indices=[],
                llm_model="",
                llm_status="no_context",
                retrieval_ms=0.0,
                generation_ms=0.0,
            )

        # 1. Retrieve
        t0 = time.perf_counter()
        hits = self._retriever.search(
            question,
            top_k=top_k or self._top_k,
            section_types=section_types,
            schedule_code_like=schedule_code_like,
            source_pdf_like=source_pdf_like,
            min_similarity=min_similarity,
        )
        retrieval_ms = (time.perf_counter() - t0) * 1000.0

        if not hits:
            return RagAnswer(
                question=question,
                answer="I could not find this in the indexed documents.",
                hits=[],
                cited_indices=[],
                llm_model="",
                llm_status="no_context",
                retrieval_ms=retrieval_ms,
                generation_ms=0.0,
            )

        # 2. Build prompt
        prompt = build_prompt(
            question,
            hits,
            max_context_chars=self._max_context_chars,
            max_excerpt_chars=self._max_excerpt_chars,
        )

        # 3. Call LLM (plain text — no JSON parsing)
        t1 = time.perf_counter()
        run_result = self._orchestrator.generate_text(
            self._generation_role,
            prompt,
            subject_kind="rag_answer",
            subject_id="0",
            stage="rag_generation",
        )
        generation_ms = (time.perf_counter() - t1) * 1000.0

        # OllamaRunResult.result is the raw response string when called via
        # generate_text (json_mode=False). For other modes it could be a
        # Pydantic model, so coerce defensively.
        result_val = getattr(run_result, "result", None)
        raw_text = result_val if isinstance(result_val, str) else ""
        status = getattr(run_result, "status", "unknown")
        model = getattr(run_result, "model", "")

        # 4. Parse citations
        cited = _parse_citations(raw_text, max_n=len(hits))

        return RagAnswer(
            question=question,
            answer=raw_text.strip(),
            hits=hits,
            cited_indices=cited,
            llm_model=model,
            llm_status=status,
            retrieval_ms=retrieval_ms,
            generation_ms=generation_ms,
            prompt=prompt if self._include_prompt else "",
        )
