"""Unit tests for RagGenerator — uses mocked retriever + mocked orchestrator
so no Ollama / sqlite is needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from duke_rates.document_intelligence.rag_generator import (
    RagAnswer,
    RagGenerator,
    _parse_citations,
    build_prompt,
)
from duke_rates.document_intelligence.rag_retriever import RetrievalHit


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _hit(rank: int, sim: float = 0.5, text: str = "sample text") -> RetrievalHit:
    return RetrievalHit(
        source_pdf=f"file_{rank}.pdf",
        section_index=0,
        start_page=rank,
        end_page=rank,
        similarity=sim,
        section_type="rate_schedule",
        section_type_source="gold",
        section_type_conf=0.9,
        schedule_codes=[f"S{rank}"],
        rider_codes=[],
        leaf_numbers=[],
        text_excerpt=text,
    )


class _FakeRetriever:
    def __init__(self, hits: list[RetrievalHit]) -> None:
        self._hits = hits
        self.last_call_kwargs: dict[str, Any] = {}

    def search(self, query: str, **kwargs: Any) -> list[RetrievalHit]:
        self.last_call_kwargs = {"query": query, **kwargs}
        return list(self._hits)


@dataclass
class _FakeRunResult:
    role: str = "balanced_classifier"
    model: str = "qwen3:8b"
    status: str = "ok"
    duration_ms: int = 100
    result: Any = ""


class _FakeOrch:
    def __init__(self, response_text: str = "", status: str = "ok") -> None:
        self._response = response_text
        self._status = status
        self.last_prompt: str = ""
        self.call_count: int = 0

    def generate_text(self, role: str, prompt: str, **kwargs: Any):
        self.call_count += 1
        self.last_prompt = prompt
        return _FakeRunResult(
            role=role,
            model="qwen3:8b",
            status=self._status,
            result=self._response,
        )


# ----------------------------------------------------------------------
# build_prompt + _parse_citations
# ----------------------------------------------------------------------


class TestParseCitations:
    def test_extracts_simple(self) -> None:
        assert _parse_citations("This is [1] and [2]", max_n=5) == [1, 2]

    def test_deduplicates(self) -> None:
        assert _parse_citations("see [1] and [1] again [2]", max_n=5) == [1, 2]

    def test_bounded_by_max_n(self) -> None:
        # [9] is out of range when max_n=5
        assert _parse_citations("[3] and [9]", max_n=5) == [3]

    def test_no_citations(self) -> None:
        assert _parse_citations("no citations here", max_n=5) == []

    def test_zero_not_valid(self) -> None:
        # 1-based; [0] is not a valid citation
        assert _parse_citations("see [0] please", max_n=5) == []

    def test_preserves_order(self) -> None:
        assert _parse_citations("[3] [1] [2]", max_n=5) == [3, 1, 2]

    def test_accepts_n_prefix_format(self) -> None:
        # qwen2.5:7b-instruct fallback uses [N1] instead of [1]
        assert _parse_citations("see [N1] and [N2]", max_n=5) == [1, 2]

    def test_accepts_ref_prefix_format(self) -> None:
        assert _parse_citations("see [Ref 1] and [Ref 2]", max_n=5) == [1, 2]

    def test_mixed_formats_in_one_answer(self) -> None:
        # Pathological but possible — accept all variants
        assert _parse_citations("[1] and [N2] and [Ref 3]", max_n=5) == [1, 2, 3]


class TestBuildPrompt:
    def test_includes_question(self) -> None:
        prompt = build_prompt("What is X?", [_hit(1)])
        assert "What is X?" in prompt

    def test_includes_numbered_blocks(self) -> None:
        prompt = build_prompt("q", [_hit(1), _hit(2)])
        assert "[1]" in prompt
        assert "[2]" in prompt

    def test_excerpt_truncation(self) -> None:
        long_text = "a" * 1500
        prompt = build_prompt(
            "q", [_hit(1, text=long_text)], max_excerpt_chars=100
        )
        # truncated excerpt followed by ellipsis
        assert "..." in prompt
        # full 1500 chars not present
        assert "a" * 1500 not in prompt

    def test_max_context_chars_drops_late_blocks(self) -> None:
        hits = [_hit(i, text="x" * 500) for i in range(1, 11)]
        prompt = build_prompt("q", hits, max_context_chars=1500, max_excerpt_chars=500)
        # At most first 3-4 blocks fit; not all 10
        assert "[1]" in prompt
        assert "[10]" not in prompt

    def test_first_block_always_included_even_if_oversize(self) -> None:
        # Even if first block alone exceeds the budget, it gets included.
        prompt = build_prompt(
            "q",
            [_hit(1, text="x" * 5000)],
            max_context_chars=100,
            max_excerpt_chars=5000,
        )
        assert "[1]" in prompt

    def test_includes_citation_string(self) -> None:
        prompt = build_prompt("q", [_hit(1)])
        assert "file_1.pdf" in prompt
        assert "Sch S1" in prompt


# ----------------------------------------------------------------------
# RagGenerator
# ----------------------------------------------------------------------


class TestRagGenerator:
    def test_happy_path(self) -> None:
        ret = _FakeRetriever([_hit(1), _hit(2)])
        orch = _FakeOrch(response_text="The answer is foo [1] and bar [2].")
        gen = RagGenerator(ret, orch)
        out = gen.answer("Question?")
        assert out.answer == "The answer is foo [1] and bar [2]."
        assert out.cited_indices == [1, 2]
        assert out.llm_status == "ok"
        assert out.llm_model == "qwen3:8b"
        assert out.is_grounded
        assert len(out.hits) == 2

    def test_empty_question_short_circuits(self) -> None:
        ret = _FakeRetriever([_hit(1)])
        orch = _FakeOrch(response_text="x")
        gen = RagGenerator(ret, orch)
        out = gen.answer("")
        assert orch.call_count == 0
        assert out.llm_status == "no_context"

    def test_no_hits_returns_canned_response_no_llm_call(self) -> None:
        ret = _FakeRetriever([])
        orch = _FakeOrch(response_text="should not be called")
        gen = RagGenerator(ret, orch)
        out = gen.answer("Question?")
        assert orch.call_count == 0
        assert out.llm_status == "no_context"
        assert "could not find" in out.answer.lower()
        assert out.hits == []
        assert out.cited_indices == []

    def test_uncited_answer_is_not_grounded(self) -> None:
        ret = _FakeRetriever([_hit(1)])
        orch = _FakeOrch(response_text="No citations in this answer.")
        gen = RagGenerator(ret, orch)
        out = gen.answer("Q?")
        assert not out.is_grounded
        assert out.cited_indices == []

    def test_out_of_range_citation_dropped(self) -> None:
        ret = _FakeRetriever([_hit(1)])
        orch = _FakeOrch(response_text="See [1] and [5] both.")
        gen = RagGenerator(ret, orch)
        out = gen.answer("Q?")
        # Only 1 hit retrieved, so [5] is dropped
        assert out.cited_indices == [1]

    def test_filters_forwarded_to_retriever(self) -> None:
        ret = _FakeRetriever([_hit(1)])
        orch = _FakeOrch(response_text="[1]")
        gen = RagGenerator(ret, orch)
        gen.answer(
            "Q",
            top_k=5,
            section_types=["rider"],
            schedule_code_like="FCAR",
            source_pdf_like="dep",
            min_similarity=0.4,
        )
        assert ret.last_call_kwargs == {
            "query": "Q",
            "top_k": 5,
            "section_types": ["rider"],
            "schedule_code_like": "FCAR",
            "source_pdf_like": "dep",
            "min_similarity": 0.4,
        }

    def test_prompt_only_included_when_requested(self) -> None:
        ret = _FakeRetriever([_hit(1)])
        orch = _FakeOrch(response_text="[1]")
        gen = RagGenerator(ret, orch)  # include_prompt=False default
        out = gen.answer("Q?")
        assert out.prompt == ""

        gen2 = RagGenerator(ret, orch, include_prompt=True)
        out2 = gen2.answer("Q?")
        assert out2.prompt != ""
        assert "Q?" in out2.prompt

    def test_cited_hits_returns_tuples(self) -> None:
        ret = _FakeRetriever([_hit(1), _hit(2), _hit(3)])
        orch = _FakeOrch(response_text="[1] and [3]")
        gen = RagGenerator(ret, orch)
        out = gen.answer("Q?")
        cited = out.cited_hits()
        assert len(cited) == 2
        assert cited[0][0] == 1
        assert cited[1][0] == 3
        uncited = out.uncited_hits()
        assert len(uncited) == 1
        assert uncited[0][0] == 2

    def test_timeout_status_propagates(self) -> None:
        ret = _FakeRetriever([_hit(1)])
        orch = _FakeOrch(response_text="", status="timeout")
        gen = RagGenerator(ret, orch)
        out = gen.answer("Q?")
        assert out.llm_status == "timeout"
        assert out.answer == ""

    def test_non_string_result_handled(self) -> None:
        # Defensive: orchestrator might return a Pydantic model in other
        # modes. Our code should coerce to empty string, not crash.
        class _Pydantic:
            pass

        ret = _FakeRetriever([_hit(1)])
        orch = _FakeOrch(response_text="")
        gen = RagGenerator(ret, orch)
        # Manually substitute the run-result.result to non-string
        original = orch.generate_text

        def patched(role, prompt, **kw):
            r = original(role, prompt, **kw)
            r.result = _Pydantic()
            return r

        orch.generate_text = patched
        out = gen.answer("Q?")
        assert out.answer == ""
        assert out.llm_status == "ok"
