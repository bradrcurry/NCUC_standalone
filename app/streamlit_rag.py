"""Streamlit UI for the NCUC tariff RAG.

Run with::

    streamlit run app/streamlit_rag.py

Single-page chat-style interface:
  - Sidebar: filters (section_types, schedule_code, source_pdf, top_k)
  - Main: question input → answer with [N] citations → expandable source panel
  - Honest signals: grounded badge, retrieval/generation timing,
    "could not find" responses surface as a yellow warning instead of red error.

Built on the existing RagRetriever + RagGenerator. No new RAG logic — this is
purely a presentation layer.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import streamlit as st  # noqa: E402

from duke_rates.config import get_settings  # noqa: E402
from duke_rates.document_intelligence.ollama_orchestrator import (  # noqa: E402
    OllamaOrchestrator,
)
from duke_rates.document_intelligence.rag_generator import RagGenerator  # noqa: E402
from duke_rates.document_intelligence.rag_retriever import RagRetriever  # noqa: E402


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

st.set_page_config(
    page_title="NCUC Tariff RAG",
    page_icon="📜",
    layout="wide",
)

SECTION_TYPE_OPTIONS = [
    "rate_schedule",
    "rider",
    "terms_conditions",
    "procedural",
    "cover_letter",
]


# ----------------------------------------------------------------------
# Cached resources
# ----------------------------------------------------------------------


@st.cache_resource(show_spinner="Loading retriever + reference embeddings…")
def get_retriever_and_generator(embedding_role: str, generation_role: str):
    """Build a (retriever, generator) pair. Cached across runs/users."""
    settings = get_settings()
    orch = OllamaOrchestrator()
    if embedding_role not in orch._roles:
        st.error(f"Embedding role {embedding_role!r} not configured in ollama_models.yaml.")
        st.stop()
    if generation_role not in orch._roles:
        st.error(f"Generation role {generation_role!r} not configured in ollama_models.yaml.")
        st.stop()
    retriever = RagRetriever(
        db_path=settings.database_path,
        orchestrator=orch,
        model_role=embedding_role,
    )
    generator = RagGenerator(
        retriever=retriever,
        orchestrator=orch,
        generation_role=generation_role,
    )
    return retriever, generator


@st.cache_data(show_spinner=False)
def get_corpus_stats() -> dict[str, int]:
    """Quick stats for the sidebar header."""
    import sqlite3
    settings = get_settings()
    conn = sqlite3.connect(str(settings.database_path))
    try:
        n_emb = conn.execute("SELECT COUNT(*) FROM section_embeddings").fetchone()[0]
        n_gold = conn.execute(
            "SELECT COUNT(*) FROM section_type_gold WHERE superseded_by IS NULL"
        ).fetchone()[0]
        n_pdfs = conn.execute(
            "SELECT COUNT(DISTINCT source_pdf) FROM section_embeddings"
        ).fetchone()[0]
    finally:
        conn.close()
    return {"embeddings": n_emb, "gold": n_gold, "pdfs": n_pdfs}


# ----------------------------------------------------------------------
# Sidebar — filters + run settings
# ----------------------------------------------------------------------

st.sidebar.title("📜 NCUC Tariff RAG")
st.sidebar.caption("Retrieval-augmented Q&A over Duke NC tariff sections.")

stats = get_corpus_stats()
st.sidebar.metric("Sections indexed", f"{stats['embeddings']:,}")
st.sidebar.metric("Gold-labeled", f"{stats['gold']:,}")
st.sidebar.metric("Distinct PDFs", f"{stats['pdfs']:,}")
st.sidebar.divider()

st.sidebar.subheader("Filters")
selected_types = st.sidebar.multiselect(
    "Section types",
    SECTION_TYPE_OPTIONS,
    default=[],
    help="Restrict retrieval to these section types. Leave empty for any.",
)
schedule_code = st.sidebar.text_input(
    "Schedule code (substring)",
    value="",
    help="E.g. 'RES', 'LGS', 'R-TOU'. Case-insensitive substring match.",
)
source_pdf = st.sidebar.text_input(
    "Source PDF (substring)",
    value="",
    help="E.g. 'sub-1305'. Restricts retrieval to PDF paths containing this.",
)

st.sidebar.divider()
st.sidebar.subheader("Settings")
top_k = st.sidebar.slider("Top-K retrieved", min_value=3, max_value=20, value=8)
generation_mode = st.sidebar.radio(
    "Mode",
    ["Retrieve + answer", "Retrieve only"],
    help="Retrieve only is much faster (no LLM call).",
)
embedding_role = st.sidebar.selectbox(
    "Embedding role",
    ["embedding_primary", "embedding_secondary"],
    help="Which model's section_embeddings to search over.",
)
generation_role = st.sidebar.selectbox(
    "Generation role",
    ["balanced_classifier", "fast_classifier", "heavy_reasoning"],
    help="LLM role used for synthesis. Faster = lower quality.",
)


# ----------------------------------------------------------------------
# Main — question input + results
# ----------------------------------------------------------------------

st.title("Ask the NCUC tariff corpus")
st.caption(
    "Answers are synthesized **only from retrieved tariff sections** and cite "
    "the source pages. If the answer isn't in the indexed documents, the "
    "system will say so rather than guess."
)

example_questions = [
    "What is the storm recovery rider on leaf 607?",
    "What are the kWh rates for residential time-of-use R-TOU-CPP?",
    "What is the Rider BA fuel charge adjustment summary?",
    "What is the demand charge for Large General Service?",
]
example_cols = st.columns(len(example_questions))
for col, q in zip(example_cols, example_questions):
    if col.button(q, use_container_width=True):
        st.session_state["question"] = q

question = st.text_area(
    "Your question",
    key="question",
    height=80,
    placeholder="e.g. What is the basic customer charge in Schedule RES?",
)

run_clicked = st.button("Search", type="primary", disabled=not question.strip())


# ----------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------


def _render_hit(hit, rank: int, *, is_cited: bool = False) -> None:
    """One hit panel."""
    border_style = "1px solid #4CAF50" if is_cited else "1px solid #888"
    bg = "#f0f9f1" if is_cited else "transparent"
    badge = "📌 cited" if is_cited else ""
    sec_type = hit.section_type or "?"
    sec_source = f"({hit.section_type_source})" if hit.section_type else ""
    codes_parts: list[str] = []
    if hit.schedule_codes:
        codes_parts.append(f"sched={','.join(hit.schedule_codes[:3])}")
    if hit.rider_codes:
        codes_parts.append(f"rider={','.join(hit.rider_codes[:3])}")
    if hit.leaf_numbers:
        codes_parts.append(f"leaf={','.join(hit.leaf_numbers[:3])}")
    codes_line = " · ".join(codes_parts) if codes_parts else ""

    with st.container(border=True):
        cols = st.columns([5, 2, 1])
        cols[0].markdown(f"**[{rank}] {hit.citation()}**  {badge}")
        cols[1].caption(f"{sec_type} {sec_source}")
        cols[2].caption(f"sim {hit.similarity:.3f}")
        if codes_line:
            st.caption(codes_line)
        excerpt = hit.text_excerpt or "(no text)"
        # Show first 600 chars by default, expandable to full
        if len(excerpt) > 600:
            with st.expander("Text excerpt", expanded=False):
                st.text(excerpt)
        else:
            st.text(excerpt)


if run_clicked and question.strip():
    retriever, generator = get_retriever_and_generator(embedding_role, generation_role)

    if generation_mode == "Retrieve only":
        with st.spinner("Retrieving…"):
            t0 = time.perf_counter()
            hits = retriever.search(
                question.strip(),
                top_k=top_k,
                section_types=selected_types or None,
                schedule_code_like=schedule_code or None,
                source_pdf_like=source_pdf or None,
            )
            elapsed = (time.perf_counter() - t0) * 1000

        st.subheader(f"Retrieved {len(hits)} sections")
        st.caption(f"retrieval: {elapsed:.0f}ms · no LLM call")
        if not hits:
            st.warning("No matching sections found.")
        else:
            for i, h in enumerate(hits, 1):
                _render_hit(h, i)
    else:
        with st.spinner("Retrieving + synthesizing…"):
            answer = generator.answer(
                question.strip(),
                top_k=top_k,
                section_types=selected_types or None,
                schedule_code_like=schedule_code or None,
                source_pdf_like=source_pdf or None,
            )

        # Status row
        status_cols = st.columns(4)
        if answer.answer.lower().strip().startswith("i could not find"):
            status_cols[0].warning("📭 No answer in corpus")
        elif answer.is_grounded:
            status_cols[0].success("✅ Grounded")
        else:
            status_cols[0].warning("⚠️ Ungrounded answer")
        status_cols[1].metric("Retrieval", f"{answer.retrieval_ms:.0f}ms")
        status_cols[2].metric("Generation", f"{answer.generation_ms:.0f}ms")
        status_cols[3].metric("LLM", answer.llm_model or "—")

        # Answer
        st.subheader("Answer")
        if answer.answer:
            st.write(answer.answer)
        else:
            st.warning("(empty answer — LLM may have errored)")

        # Cited sources
        cited = answer.cited_hits()
        if cited:
            st.subheader(f"Cited sources ({len(cited)})")
            for rank, h in cited:
                _render_hit(h, rank, is_cited=True)

        # Uncited (collapsed by default)
        uncited = answer.uncited_hits()
        if uncited:
            with st.expander(f"Retrieved but not cited ({len(uncited)})", expanded=False):
                for rank, h in uncited:
                    _render_hit(h, rank)


# Footer
st.divider()
st.caption(
    "Built on `RagRetriever` + `RagGenerator`. Same logic as `doc-intel "
    "rag-answer` on the CLI. Section embeddings via Ollama; answers via local LLM."
)
