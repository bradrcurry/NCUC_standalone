"""Unit tests for the RAG eval harness."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from duke_rates.document_intelligence.rag_eval import (
    EvalCase,
    EvalReport,
    load_eval_set,
    score_generation,
    score_retrieval,
)
from duke_rates.document_intelligence.rag_generator import RagAnswer
from duke_rates.document_intelligence.rag_retriever import RetrievalHit


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _hit(
    rank: int,
    sim: float = 0.5,
    leaf: list[str] | None = None,
    sched: list[str] | None = None,
    pdf: str | None = None,
) -> RetrievalHit:
    return RetrievalHit(
        source_pdf=pdf or f"file_{rank}.pdf",
        section_index=0,
        start_page=rank,
        end_page=rank,
        similarity=sim,
        section_type="rate_schedule",
        section_type_source="gold",
        section_type_conf=0.9,
        schedule_codes=sched or [],
        rider_codes=[],
        leaf_numbers=leaf or [],
        text_excerpt="sample",
    )


def _case(**overrides) -> EvalCase:
    base = {
        "id": "t1",
        "question": "Q?",
    }
    base.update(overrides)
    return EvalCase(**base)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Loader
# ----------------------------------------------------------------------


class TestLoader:
    def test_loads_eval_set_yaml(self, tmp_path: Path) -> None:
        yaml_content = """
version: 1
cases:
  - id: a
    question: "What is X?"
    section_types: [rider]
    expected_leaf_numbers: [607]
    expected_answer_keywords: [storm]
  - id: b
    question: "Off-topic"
    expected_no_answer: true
"""
        p = tmp_path / "eval.yaml"
        p.write_text(yaml_content, encoding="utf-8")
        cases = load_eval_set(p)
        assert len(cases) == 2
        assert cases[0].id == "a"
        assert cases[0].section_types == ["rider"]
        assert cases[0].expected_leaf_numbers == ["607"]  # coerced to str
        assert cases[1].expected_no_answer is True

    def test_missing_cases_key_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text("version: 1\n", encoding="utf-8")
        with pytest.raises(ValueError, match="missing 'cases'"):
            load_eval_set(p)

    def test_repo_eval_set_loads(self) -> None:
        # The shipped eval set must always be loadable + non-empty.
        p = Path(__file__).parent / "rag_eval_set.yaml"
        assert p.exists()
        cases = load_eval_set(p)
        assert len(cases) >= 10


# ----------------------------------------------------------------------
# score_retrieval
# ----------------------------------------------------------------------


class TestScoreRetrieval:
    def test_no_target_returns_none_rank(self) -> None:
        case = _case()
        result = score_retrieval(case, [_hit(1)])
        assert result.expected_rank is None
        assert result.matched_via is None

    def test_leaf_number_match_in_first_hit(self) -> None:
        case = _case(expected_leaf_numbers=["607"])
        hits = [_hit(1, leaf=["607"])]
        result = score_retrieval(case, hits)
        assert result.expected_rank == 1
        assert result.matched_via == "leaf"

    def test_leaf_number_match_later_rank(self) -> None:
        case = _case(expected_leaf_numbers=["607"])
        hits = [_hit(1), _hit(2), _hit(3, leaf=["607"])]
        result = score_retrieval(case, hits)
        assert result.expected_rank == 3

    def test_schedule_code_match(self) -> None:
        case = _case(expected_schedule_codes=["RES"])
        hits = [_hit(1), _hit(2, sched=["RES"])]
        result = score_retrieval(case, hits)
        assert result.expected_rank == 2
        assert result.matched_via == "schedule"

    def test_schedule_code_case_insensitive(self) -> None:
        case = _case(expected_schedule_codes=["res"])
        hits = [_hit(1, sched=["RES"])]
        result = score_retrieval(case, hits)
        assert result.expected_rank == 1

    def test_source_pdf_substring_match(self) -> None:
        case = _case(expected_source_pdf_substrings=["sub_1234"])
        hits = [_hit(1, pdf="E-2_Sub_1234_filing.pdf")]
        result = score_retrieval(case, hits)
        assert result.expected_rank == 1
        assert result.matched_via == "source_pdf"

    def test_no_match_returns_none(self) -> None:
        case = _case(expected_leaf_numbers=["999"])
        hits = [_hit(1, leaf=["100"])]
        result = score_retrieval(case, hits)
        assert result.expected_rank is None
        assert result.matched_via is None

    def test_top1_similarity_recorded_even_when_no_match(self) -> None:
        case = _case(expected_leaf_numbers=["999"])
        hits = [_hit(1, sim=0.5)]
        result = score_retrieval(case, hits)
        assert result.top1_similarity == 0.5
        assert result.expected_rank is None

    def test_empty_hits(self) -> None:
        case = _case(expected_leaf_numbers=["607"])
        result = score_retrieval(case, [])
        assert result.expected_rank is None
        assert result.top1_similarity is None
        assert result.hits_returned == 0


# ----------------------------------------------------------------------
# score_generation
# ----------------------------------------------------------------------


def _answer(text: str, cited: list[int] | None = None) -> RagAnswer:
    return RagAnswer(
        question="q",
        answer=text,
        hits=[],
        cited_indices=cited or [],
        llm_model="qwen3:8b",
        llm_status="ok",
        retrieval_ms=10.0,
        generation_ms=100.0,
    )


class TestScoreGeneration:
    def test_grounded_answer_with_keywords(self) -> None:
        case = _case(expected_answer_keywords=["storm", "rider"])
        ans = _answer("The storm recovery rider applies [1]", cited=[1])
        out = score_generation(case, ans)
        assert out.answered is True
        assert out.grounded is True
        assert set(out.keyword_matches) == {"storm", "rider"}

    def test_refusal_detected(self) -> None:
        case = _case()
        ans = _answer("I could not find this in the indexed documents.")
        out = score_generation(case, ans)
        assert out.answered is False

    def test_refusal_case_insensitive(self) -> None:
        case = _case()
        ans = _answer("I COULD NOT FIND THIS IN THE INDEXED DOCUMENTS.")
        out = score_generation(case, ans)
        assert out.answered is False

    def test_ungrounded_when_no_citations(self) -> None:
        case = _case()
        ans = _answer("Some answer with no brackets", cited=[])
        out = score_generation(case, ans)
        assert out.answered is True
        assert out.grounded is False

    def test_refusal_correct_when_expected(self) -> None:
        case = _case(expected_no_answer=True)
        ans = _answer("I could not find this in the indexed documents.")
        out = score_generation(case, ans)
        assert out.expected_refusal_correct is True

    def test_refusal_wrong_when_expected_but_answered(self) -> None:
        case = _case(expected_no_answer=True)
        ans = _answer("The price is $42 [1]", cited=[1])
        out = score_generation(case, ans)
        assert out.expected_refusal_correct is False

    def test_refusal_not_meaningful_when_answer_expected(self) -> None:
        case = _case(expected_answer_keywords=["foo"])
        ans = _answer("foo bar [1]", cited=[1])
        out = score_generation(case, ans)
        assert out.expected_refusal_correct is None

    def test_keyword_match_case_insensitive(self) -> None:
        case = _case(expected_answer_keywords=["RIDER"])
        ans = _answer("The rider applies [1]", cited=[1])
        out = score_generation(case, ans)
        assert "RIDER" in out.keyword_matches

    def test_partial_keyword_match(self) -> None:
        case = _case(expected_answer_keywords=["storm", "missing"])
        ans = _answer("storm recovery [1]", cited=[1])
        out = score_generation(case, ans)
        assert out.keyword_matches == ["storm"]


# ----------------------------------------------------------------------
# EvalReport aggregation
# ----------------------------------------------------------------------


class TestEvalReport:
    def test_retrieval_metrics_basic(self) -> None:
        cases = [
            _case(id="a", expected_leaf_numbers=["1"]),
            _case(id="b", expected_leaf_numbers=["2"]),
            _case(id="c", expected_leaf_numbers=["3"]),
        ]
        retrieval = [
            score_retrieval(cases[0], [_hit(1, leaf=["1"])]),  # rank 1
            score_retrieval(cases[1], [_hit(1), _hit(2), _hit(3, leaf=["2"])]),  # rank 3
            score_retrieval(cases[2], [_hit(1)]),  # not found
        ]
        report = EvalReport(cases=cases, retrieval_results=retrieval)
        m = report.retrieval_metrics()
        assert m["n_cases"] == 3
        assert m["recall_at_5"] == round(2 / 3, 3)  # a and b
        assert m["mrr_at_10"] == round((1.0 + 1.0 / 3) / 3, 3)

    def test_retrieval_metrics_ignores_refusal_cases(self) -> None:
        cases = [
            _case(id="a", expected_leaf_numbers=["1"]),
            _case(id="b", expected_no_answer=True),  # no retrieval target
        ]
        retrieval = [
            score_retrieval(cases[0], [_hit(1, leaf=["1"])]),
            score_retrieval(cases[1], [_hit(1)]),
        ]
        report = EvalReport(cases=cases, retrieval_results=retrieval)
        m = report.retrieval_metrics()
        # Only case "a" is in scope
        assert m["n_cases"] == 1
        assert m["recall_at_5"] == 1.0

    def test_generation_metrics(self) -> None:
        cases = [
            _case(
                id="a",
                expected_leaf_numbers=["1"],
                expected_answer_keywords=["storm"],
            ),
            _case(id="b", expected_no_answer=True),
            _case(id="c", expected_no_answer=True),
        ]
        retrieval = [
            score_retrieval(cases[0], [_hit(1, leaf=["1"])]),
            score_retrieval(cases[1], [_hit(1)]),
            score_retrieval(cases[2], [_hit(1)]),
        ]
        generation = [
            score_generation(cases[0], _answer("storm result [1]", cited=[1])),
            score_generation(cases[1], _answer("I could not find this in the indexed documents.")),
            score_generation(cases[2], _answer("Wrong, the answer is X [1]", cited=[1])),
        ]
        report = EvalReport(
            cases=cases,
            retrieval_results=retrieval,
            generation_results=generation,
        )
        gm = report.generation_metrics()
        assert gm["n_answered"] == 2  # a and c (b refused)
        assert gm["n_refusal_expected"] == 2  # b and c
        assert gm["grounded_rate"] == 1.0  # both answered cases had citations
        assert gm["keyword_match_rate"] == 1.0  # a matched 'storm'
        assert gm["correct_refusal_rate"] == 0.5  # b correct, c wrong

    def test_metrics_with_no_results(self) -> None:
        report = EvalReport(cases=[], retrieval_results=[])
        m = report.retrieval_metrics()
        assert m["n_cases"] == 0
        assert m["recall_at_5"] is None
