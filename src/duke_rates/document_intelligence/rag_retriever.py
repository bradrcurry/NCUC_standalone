"""Section-level RAG retriever over section_embeddings.

The retriever takes a natural-language query, embeds it with the same model
used for section_embeddings, runs cosine similarity against all reference
vectors, applies optional metadata filters (section_type, schedule_code,
source_pdf), and returns the top-k matches with citation-grade metadata
and a text excerpt.

This is the R1 layer of the RAG stack: pure retrieval, no generation. It
exists to validate that the section_embeddings produce sensible top-k
hits for real questions before generation cost is added in R2.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from duke_rates.document_intelligence.section_text_extractor import (
    fetch_section_text,
)


# Default section_type filter — exclude empties; include all known types.
_DEFAULT_SECTION_TYPES = frozenset(
    {
        "rate_schedule",
        "rider",
        "terms_conditions",
        "procedural",
        "cover_letter",
    }
)


@dataclass(frozen=True)
class RetrievalHit:
    """One retrieval result.

    ``section_type_source`` is ``'gold'`` when the label came from
    section_type_gold, ``'predicted'`` when from section_knn_v1, or
    ``'heuristic'`` when only document_sections.section_type was available.
    """

    source_pdf: str
    section_index: int
    start_page: int
    end_page: int
    similarity: float
    section_type: str | None
    section_type_source: str  # 'gold' | 'predicted' | 'heuristic' | 'none'
    section_type_conf: float | None
    schedule_codes: list[str] = field(default_factory=list)
    rider_codes: list[str] = field(default_factory=list)
    leaf_numbers: list[str] = field(default_factory=list)
    text_excerpt: str = ""

    def citation(self) -> str:
        """Format a short citation: filename + page range + best code."""
        pdf_name = Path(self.source_pdf).name
        code = ""
        if self.schedule_codes:
            code = f" Sch {self.schedule_codes[0]}"
        elif self.rider_codes:
            code = f" Rider {self.rider_codes[0]}"
        elif self.leaf_numbers:
            code = f" leaf {self.leaf_numbers[0]}"
        return f"{pdf_name}{code} p{self.start_page}-{self.end_page}"


def _parse_json_list(blob: str | None) -> list[str]:
    if not blob or blob in ("[]", "null"):
        return []
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data]


class RagRetriever:
    """Cosine-similarity retriever over section_embeddings.

    Parameters
    ----------
    db_path : Path
        SQLite database with section_embeddings + document_sections +
        section_type_gold + document_classifications.
    orchestrator : Any
        Provides ``embed(role, text) -> list[float]`` and
        ``_roles[role].primary`` for the active model name.
    model_role : str
        Orchestrator role to use (default 'embedding_primary').
    embedding_kind : str
        Which embedding flavor to retrieve (default 'section_text').
    excerpt_chars : int
        How many chars of section text to include in each hit (default 400).
    """

    def __init__(
        self,
        db_path: Path,
        orchestrator: Any,
        *,
        model_role: str = "embedding_primary",
        embedding_kind: str = "section_text",
        excerpt_chars: int = 400,
    ) -> None:
        self._db_path = db_path
        self._orchestrator = orchestrator
        self._model_role = model_role
        self._embedding_kind = embedding_kind
        self._excerpt_chars = excerpt_chars

        # Cached reference table — built lazily on first search().
        self._loaded_model: str | None = None
        self._ref_vectors: np.ndarray | None = None
        self._ref_meta: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        section_types: list[str] | None = None,
        schedule_code_like: str | None = None,
        source_pdf_like: str | None = None,
        min_similarity: float = 0.0,
    ) -> list[RetrievalHit]:
        """Run a single retrieval.

        Filters are applied **before** the top-k truncation so the result
        is always the best-k matches satisfying every filter.
        """
        if not query or not query.strip():
            return []

        # 1. Embed query
        query_vec = np.array(
            self._orchestrator.embed(self._model_role, query),
            dtype=np.float32,
        )
        q_norm = query_vec / (np.linalg.norm(query_vec) + 1e-10)

        # 2. Load reference table (cached)
        self._ensure_loaded()
        if self._ref_vectors is None or len(self._ref_vectors) == 0:
            return []

        # 3. Cosine sim across all rows
        ref_norms = self._ref_vectors / (
            np.linalg.norm(self._ref_vectors, axis=1, keepdims=True) + 1e-10
        )
        sims = ref_norms @ q_norm  # shape (N,)

        # 4. Filter
        allowed_types = (
            set(section_types) if section_types else None
        )
        sched_like = schedule_code_like.upper() if schedule_code_like else None
        pdf_like = source_pdf_like.lower() if source_pdf_like else None

        candidate_indices: list[int] = []
        for i, meta in enumerate(self._ref_meta):
            if sims[i] < min_similarity:
                continue
            if allowed_types is not None:
                if meta["section_type"] not in allowed_types:
                    continue
            if sched_like is not None:
                if not any(sched_like in c.upper() for c in meta["schedule_codes"]):
                    continue
            if pdf_like is not None:
                if pdf_like not in meta["source_pdf"].lower():
                    continue
            candidate_indices.append(i)

        if not candidate_indices:
            return []

        # 5. Top-k by similarity
        candidate_sims = sims[candidate_indices]
        k = min(top_k, len(candidate_sims))
        top_local = np.argpartition(-candidate_sims, k - 1)[:k]
        top_local = top_local[np.argsort(-candidate_sims[top_local])]
        top_indices = [candidate_indices[i] for i in top_local]

        # 6. Hydrate text excerpts (only for the top-k, one connection)
        conn = sqlite3.connect(str(self._db_path))
        try:
            hits: list[RetrievalHit] = []
            for idx in top_indices:
                meta = self._ref_meta[idx]
                section_text = fetch_section_text(
                    conn,
                    meta["source_pdf"],
                    meta["start_page"],
                    meta["end_page"],
                    max_chars=self._excerpt_chars,
                )
                hits.append(
                    RetrievalHit(
                        source_pdf=meta["source_pdf"],
                        section_index=meta["section_index"],
                        start_page=meta["start_page"],
                        end_page=meta["end_page"],
                        similarity=float(sims[idx]),
                        section_type=meta["section_type"],
                        section_type_source=meta["section_type_source"],
                        section_type_conf=meta["section_type_conf"],
                        schedule_codes=meta["schedule_codes"],
                        rider_codes=meta["rider_codes"],
                        leaf_numbers=meta["leaf_numbers"],
                        text_excerpt=section_text.text,
                    )
                )
        finally:
            conn.close()
        return hits

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        model = self._orchestrator._roles[self._model_role].primary
        if self._loaded_model == model and self._ref_vectors is not None:
            return

        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Single query joins:
            #   section_embeddings (vector + page range)
            #   document_sections (codes/leaves/heuristic section_type)
            #   section_type_gold (preferred label, when present)
            #   document_classifications (KNN-predicted label, fallback)
            rows = conn.execute(
                """
                SELECT
                    e.source_pdf, e.section_index, e.start_page, e.end_page,
                    e.vector,
                    ds.section_type           AS heuristic_section_type,
                    ds.schedule_codes_json,
                    ds.rider_codes_json,
                    ds.leaf_numbers_json,
                    g.section_type            AS gold_section_type,
                    g.confidence              AS gold_conf,
                    dc.label                  AS knn_label,
                    dc.confidence             AS knn_conf
                FROM section_embeddings e
                LEFT JOIN document_sections ds
                  ON ds.source_pdf = e.source_pdf AND ds.section_index = e.section_index
                LEFT JOIN section_type_gold g
                  ON g.source_pdf = e.source_pdf AND g.section_index = e.section_index
                 AND g.superseded_by IS NULL
                LEFT JOIN document_classifications dc
                  ON dc.subject_kind = 'document_section'
                 AND dc.subject_id = CAST(ds.id AS TEXT)
                 AND dc.classifier = 'section_knn_v1'
                 AND dc.superseded_by IS NULL
                WHERE e.embedding_kind = ?
                  AND e.embedding_model = ?
                ORDER BY e.id
                """,
                (self._embedding_kind, model),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            self._ref_vectors = None
            self._ref_meta = []
            self._loaded_model = model
            return

        vectors: list[np.ndarray] = []
        meta: list[dict[str, Any]] = []
        for row in rows:
            try:
                vec = np.frombuffer(row["vector"], dtype=np.float32)
            except Exception:
                continue
            if vec.size == 0:
                continue

            # Preferred section_type: gold > knn_v1 > heuristic
            if row["gold_section_type"]:
                sec_type = row["gold_section_type"]
                source = "gold"
                conf = row["gold_conf"]
            elif row["knn_label"] and row["knn_label"] != "unknown":
                sec_type = row["knn_label"]
                source = "predicted"
                conf = row["knn_conf"]
            elif row["heuristic_section_type"]:
                sec_type = row["heuristic_section_type"]
                source = "heuristic"
                conf = None
            else:
                sec_type = None
                source = "none"
                conf = None

            vectors.append(vec)
            meta.append(
                {
                    "source_pdf": row["source_pdf"],
                    "section_index": int(row["section_index"]),
                    "start_page": int(row["start_page"]),
                    "end_page": int(row["end_page"]),
                    "section_type": sec_type,
                    "section_type_source": source,
                    "section_type_conf": float(conf) if conf is not None else None,
                    "schedule_codes": _parse_json_list(row["schedule_codes_json"]),
                    "rider_codes": _parse_json_list(row["rider_codes_json"]),
                    "leaf_numbers": _parse_json_list(row["leaf_numbers_json"]),
                }
            )

        self._ref_vectors = np.stack(vectors, axis=0)
        self._ref_meta = meta
        self._loaded_model = model
