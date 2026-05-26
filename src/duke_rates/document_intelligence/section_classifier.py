"""Section-level KNN classifier.

Predicts ``section_type`` for a section span by:
1. Embedding the section's concatenated page text.
2. Finding the top-k nearest neighbor section_embeddings by cosine similarity.
3. Looking up each neighbor's section_type from ``section_type_gold``.
4. Voting weighted by similarity × neighbor confidence.

Distinct from the doc-level ``EmbeddingKNNClassifier`` — that one labels
whole PDFs with doc_type; this one labels per-section spans with
section_type (rate_schedule, rider, terms_conditions, procedural,
cover_letter). The two classifiers complement each other: section_knn_v1
is used for bundle splitting / per-leaf attribution; the doc-level KNN
remains for whole-doc triage.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

from duke_rates.classification.result import ClassificationResult


class SectionKNNClassifier:
    """KNN classifier over section_embeddings, voting from section_type_gold.

    Parameters
    ----------
    db_path : Path
        Path to the SQLite database.
    orchestrator : OllamaOrchestrator
        Source of embeddings (must implement ``embed(role, text) -> list``).
    model_role : str
        Orchestrator role to use (default ``"embedding_primary"``).
    k : int
        Number of neighbors for KNN voting (default 9).
    min_neighbors : int
        Minimum neighbors with gold labels required to make a prediction
        (default 3).
    embedding_kind : str
        Which section text slice to embed (default ``"section_text"``).
    max_chars : int
        Truncate section text to this many characters before embedding
        (default 2000).
    """

    CLASSIFIER_NAME = "section_knn_v1"
    CLASSIFIER_VERSION = "v1"

    def __init__(
        self,
        db_path: Path,
        orchestrator: Any,
        *,
        model_role: str = "embedding_primary",
        k: int = 9,
        min_neighbors: int = 3,
        embedding_kind: str = "section_text",
        max_chars: int = 2000,
        gold_only_reference: bool = True,
    ) -> None:
        """Build a section-level KNN classifier.

        ``gold_only_reference`` (default True) restricts the reference vector
        pool at load time to sections that have an active section_type_gold
        row. This is necessary because section_embeddings is densely
        populated (one vector per section in document_sections) but only a
        small minority of sections have gold labels — without this filter,
        the top-k neighbors are mostly unlabeled and the classifier
        returns "unknown" for the vast majority of queries.
        """
        self._db_path = db_path
        self._orchestrator = orchestrator
        self._model_role = model_role
        self._k = k
        self._min_neighbors = min_neighbors
        self._embedding_kind = embedding_kind
        self._max_chars = max_chars
        self._gold_only_reference = gold_only_reference

        self._loaded_model: str | None = None
        self._loaded_kind: str | None = None
        self._ref_vectors: np.ndarray | None = None
        self._ref_keys: list[tuple[str, int]] = []  # (source_pdf, section_index)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify(
        self,
        section_text: str,
        *,
        exclude_key: tuple[str, int] | None = None,
    ) -> ClassificationResult:
        """Classify a section by its already-extracted text.

        ``exclude_key`` is an optional ``(source_pdf, section_index)`` to
        exclude from the neighbor pool (for leave-one-out evaluation of a
        section that is itself in the reference set).
        """
        if not section_text or not section_text.strip():
            return self._unknown("no_text")

        text = section_text[: self._max_chars]
        try:
            query_vec = np.array(
                self._orchestrator.embed(self._model_role, text),
                dtype=np.float32,
            )
        except Exception:
            return self._unknown("embed_failed")

        self._load_reference_vectors()
        if self._ref_vectors is None or len(self._ref_vectors) == 0:
            return self._unknown("no_reference_vectors")

        scores = self._cosine_similarity(query_vec, self._ref_vectors)

        valid_indices = list(range(len(scores)))
        if exclude_key is not None:
            valid_indices = [
                i for i in valid_indices if self._ref_keys[i] != exclude_key
            ]
        if not valid_indices:
            return self._unknown("only_self_match")

        valid_scores = scores[valid_indices]
        top_k = min(self._k, len(valid_scores))
        local_top = np.argpartition(-valid_scores, top_k - 1)[:top_k]
        local_top = local_top[np.argsort(-valid_scores[local_top])]
        top_indices = np.array([valid_indices[i] for i in local_top])

        # Returns a list aligned with top_indices; entries are None when the
        # corresponding neighbor has no active section_type_gold row.
        neighbor_labels = self._lookup_section_labels(top_indices)
        with_gold = [nbr for nbr in neighbor_labels if nbr is not None]
        if len(with_gold) < self._min_neighbors:
            return self._unknown(
                "insufficient_gold_neighbors", actual=len(with_gold)
            )

        weighted: dict[str, float] = {}
        evidence: list[dict[str, Any]] = []
        for idx, nbr in zip(top_indices, neighbor_labels):
            if nbr is None:
                continue
            sim = float(scores[idx])
            weight = sim * float(nbr.get("confidence", 0.5))
            label = nbr.get("label", "unknown")
            weighted[label] = weighted.get(label, 0.0) + weight
            evidence.append(
                {
                    "kind": "neighbor",
                    "label": label,
                    "similarity": round(sim, 4),
                    "source_pdf": nbr.get("source_pdf", ""),
                    "section_index": nbr.get("section_index", -1),
                    "neighbor_confidence": round(
                        float(nbr.get("confidence", 0.5)), 4
                    ),
                }
            )

        total_weight = sum(weighted.values())
        if total_weight <= 0:
            return self._unknown("zero_weight")

        predicted = max(weighted, key=lambda k: weighted[k])
        confidence = weighted[predicted] / total_weight
        alternatives = sorted(
            [
                (label, score / total_weight)
                for label, score in weighted.items()
                if label != predicted
            ],
            key=lambda x: x[1],
            reverse=True,
        )[:5]

        return ClassificationResult(
            label=predicted,
            confidence=round(confidence, 4),
            classifier=self.CLASSIFIER_NAME,
            classifier_version=self.CLASSIFIER_VERSION,
            evidence=evidence[:10],
            alternatives=alternatives,
            metadata={
                "model_role": self._model_role,
                "embedding_kind": self._embedding_kind,
                "k": self._k,
                "actual_neighbors": len(neighbor_labels),
            },
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_reference_vectors(self) -> None:
        model = self._orchestrator._roles[self._model_role].primary
        key = (model, self._embedding_kind, self._gold_only_reference)
        if key == (self._loaded_model, self._loaded_kind, self._gold_only_reference) and self._loaded_model is not None:
            return

        conn = sqlite3.connect(str(self._db_path))
        try:
            if self._gold_only_reference:
                rows = conn.execute(
                    """
                    SELECT e.source_pdf, e.section_index, e.vector
                    FROM section_embeddings e
                    JOIN section_type_gold g
                      ON g.source_pdf = e.source_pdf
                     AND g.section_index = e.section_index
                     AND g.superseded_by IS NULL
                    WHERE e.embedding_kind = ? AND e.embedding_model = ?
                    ORDER BY e.id
                    """,
                    (self._embedding_kind, model),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT source_pdf, section_index, vector
                    FROM section_embeddings
                    WHERE embedding_kind = ? AND embedding_model = ?
                    ORDER BY id
                    """,
                    (self._embedding_kind, model),
                ).fetchall()
        finally:
            conn.close()

        if not rows:
            self._ref_vectors = None
            self._ref_keys = []
            return

        vectors: list[np.ndarray] = []
        keys: list[tuple[str, int]] = []
        for source_pdf, section_index, blob in rows:
            try:
                vec = np.frombuffer(blob, dtype=np.float32)
                if len(vec) > 0:
                    vectors.append(vec)
                    keys.append((source_pdf, int(section_index)))
            except Exception:
                continue

        self._ref_vectors = np.stack(vectors, axis=0) if vectors else None
        self._ref_keys = keys
        self._loaded_model = model
        self._loaded_kind = self._embedding_kind

    def _cosine_similarity(
        self, query: np.ndarray, refs: np.ndarray
    ) -> np.ndarray:
        q_norm = query / (np.linalg.norm(query) + 1e-10)
        r_norms = refs / (np.linalg.norm(refs, axis=1, keepdims=True) + 1e-10)
        return r_norms @ q_norm

    def _lookup_section_labels(
        self, top_indices: np.ndarray
    ) -> list[dict[str, Any] | None]:
        """Return a list aligned with top_indices.

        Entry is ``None`` when the corresponding neighbor has no active
        section_type_gold row; callers should skip ``None`` entries while
        retaining positional alignment with ``top_indices``/scores.
        """
        top_keys = [self._ref_keys[i] for i in top_indices]
        if not top_keys:
            return []

        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            placeholders = ",".join(["(?, ?)"] * len(top_keys))
            params: list[Any] = []
            for pdf, idx in top_keys:
                params.append(pdf)
                params.append(idx)
            rows = conn.execute(
                f"""
                SELECT source_pdf, section_index, section_type, confidence
                FROM section_type_gold
                WHERE (source_pdf, section_index) IN ({placeholders})
                  AND superseded_by IS NULL
                """,
                params,
            ).fetchall()
        finally:
            conn.close()

        label_map: dict[tuple[str, int], dict[str, Any]] = {}
        for row in rows:
            label_map[(row["source_pdf"], int(row["section_index"]))] = {
                "label": row["section_type"],
                "confidence": row["confidence"] or 0.5,
                "source_pdf": row["source_pdf"],
                "section_index": int(row["section_index"]),
            }

        # Keep alignment with top_keys; None for missing-gold neighbors.
        return [label_map.get(key) for key in top_keys]

    def _unknown(self, reason: str, **extra: Any) -> ClassificationResult:
        return ClassificationResult(
            label="unknown",
            confidence=0.0,
            classifier=self.CLASSIFIER_NAME,
            classifier_version=self.CLASSIFIER_VERSION,
            evidence=[{"kind": reason, "value": 0, **(extra or {})}],
        )
