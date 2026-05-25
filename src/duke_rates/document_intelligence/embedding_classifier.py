"""
KNN classifier using cosine similarity over pre-computed embeddings (Phase 4).

Given a target PDF, embeds its text, computes cosine similarity against
all reference vectors in ``document_embeddings``, selects the top-k neighbors,
looks up their ``document_type`` labels from ``document_classifications``,
and produces a weighted ``ClassificationResult``.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import numpy as np

from duke_rates.classification.result import ClassificationResult
from duke_rates.document_intelligence.section_derived_labels import (
    fetch_section_derived_labels,
)
from duke_rates.document_intelligence.text_slicer import slice_pdf_text


# Allowed values for the label_source constructor arg.
LABEL_SOURCE_RULE_V1 = "rule_v1"
LABEL_SOURCE_SECTION_GOLD = "section_gold"
LABEL_SOURCE_SECTION_GOLD_OR_RULE = "section_gold_or_rule"

_VALID_LABEL_SOURCES = {
    LABEL_SOURCE_RULE_V1,
    LABEL_SOURCE_SECTION_GOLD,
    LABEL_SOURCE_SECTION_GOLD_OR_RULE,
}


class EmbeddingKNNClassifier:
    """KNN classifier using cosine similarity over pre-computed embeddings.

    Parameters
    ----------
    db_path : Path
        Path to the SQLite database.
    orchestrator : OllamaOrchestrator
        Orchestrator configured with embedding model roles.
    model_role : str
        Which embedding model role to use (default ``"embedding_primary"``).
    k : int
        Number of neighbors for KNN voting (default 11).
    min_neighbors : int
        Minimum neighbors required for a valid classification (default 5).
    embedding_kind : str
        Which text slice to embed and match against (default ``"full_text"``).
    max_chars : int
        Truncate query text to this many characters (default 2000).
    label_source : str
        Which source to use for neighbor doc_type labels:
        - ``"rule_v1"``: legacy, reads rule_document_type_v1 from
          document_classifications (default for back-compat).
        - ``"section_gold"``: aggregate section_type_gold rows per neighbor
          PDF into a doc_type. Only neighbors with section gold contribute;
          others vote UNKNOWN.
        - ``"section_gold_or_rule"``: prefer section_gold, fall back to
          rule_v1 for neighbors without section gold. Recommended.
    """

    def __init__(
        self,
        db_path: Path,
        orchestrator: Any,
        *,
        model_role: str = "embedding_primary",
        k: int = 11,
        min_neighbors: int = 5,
        embedding_kind: str = "full_text",
        max_chars: int = 2000,
        label_source: str = LABEL_SOURCE_RULE_V1,
    ) -> None:
        if label_source not in _VALID_LABEL_SOURCES:
            raise ValueError(
                f"Invalid label_source {label_source!r}; must be one of "
                f"{sorted(_VALID_LABEL_SOURCES)}"
            )
        self._db_path = db_path
        self._orchestrator = orchestrator
        self._model_role = model_role
        self._k = k
        self._min_neighbors = min_neighbors
        self._embedding_kind = embedding_kind
        self._max_chars = max_chars
        self._label_source = label_source

        # Cache
        self._loaded_model: str | None = None
        self._loaded_kind: str | None = None
        self._ref_vectors: np.ndarray | None = None
        self._ref_pdfs: list[str] = []
        self._ref_ids: list[int] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify(
        self,
        pdf_path: str | Path,
        metadata: dict[str, Any] | None = None,
    ) -> ClassificationResult:
        """Embed *pdf_path* and return a KNN-based document_type classification."""
        pdf_path = Path(pdf_path)

        # 1. Slice and embed the query
        slices = slice_pdf_text(pdf_path, max_chars=self._max_chars)
        text = self._get_slice(slices)
        if not text:
            return self._unknown_result("no_text")

        try:
            vector = self._orchestrator.embed(self._model_role, text)
        except Exception:
            return self._unknown_result("embed_failed")

        query_vec = np.array(vector, dtype=np.float32)

        # 2. Load reference vectors (cached)
        self._load_reference_vectors()

        if self._ref_vectors is None or len(self._ref_vectors) == 0:
            return self._unknown_result("no_reference_vectors")

        # 3. Cosine similarity
        scores = self._cosine_similarity(query_vec, self._ref_vectors)

        # 4. Top-k neighbors (exclude self-match by pdf_path)
        if len(scores) == 0:
            return self._unknown_result("no_neighbors")

        # Exclude the query document from its own reference set
        query_pdf = str(pdf_path)
        valid_indices = [
            i for i in range(len(scores))
            if self._ref_pdfs[i] != query_pdf
        ]
        if not valid_indices:
            return self._unknown_result("only_self_match")

        valid_scores = scores[valid_indices]
        top_k = min(self._k, len(valid_scores))
        if top_k < self._min_neighbors:
            return self._unknown_result(
                "insufficient_other_neighbors",
                available=len(valid_scores),
            )

        local_top = np.argpartition(-valid_scores, top_k - 1)[:top_k]
        local_top = local_top[np.argsort(-valid_scores[local_top])]
        top_indices = np.array([valid_indices[i] for i in local_top])

        # 5. Look up document_type labels
        neighbor_labels = self._lookup_labels(top_indices)

        if len(neighbor_labels) < self._min_neighbors:
            return self._unknown_result(
                "insufficient_neighbors",
                actual=len(neighbor_labels),
            )

        # 6. Weighted voting
        weighted_scores: dict[str, float] = {}
        top_evidence: list[dict] = []
        for idx, nbr in zip(top_indices, neighbor_labels):
            sim = float(scores[idx])
            weight = sim * float(nbr.get("confidence", 0.5))
            label = nbr.get("label", "UNKNOWN")
            weighted_scores[label] = weighted_scores.get(label, 0.0) + weight
            top_evidence.append(
                {
                    "kind": "neighbor",
                    "label": label,
                    "similarity": round(sim, 4),
                    "neighbor_confidence": round(float(nbr.get("confidence", 0.5)), 4),
                    "source_pdf": nbr.get("source_pdf", ""),
                    "subject_id": nbr.get("subject_id", ""),
                }
            )

        total_weight = sum(weighted_scores.values())
        if total_weight <= 0:
            return self._unknown_result("zero_weight")

        predicted = max(weighted_scores, key=lambda k: weighted_scores[k])
        confidence = weighted_scores[predicted] / total_weight

        alternatives = sorted(
            [
                (label, score / total_weight)
                for label, score in weighted_scores.items()
                if label != predicted
            ],
            key=lambda x: x[1],
            reverse=True,
        )[:5]

        return ClassificationResult(
            label=predicted,
            confidence=round(confidence, 4),
            classifier="embedding_knn_v1",
            classifier_version=self._classifier_version(),
            evidence=top_evidence[:10],
            alternatives=alternatives,
            metadata={
                "model_role": self._model_role,
                "embedding_kind": self._embedding_kind,
                "k": self._k,
                "actual_neighbors": len(neighbor_labels),
                "label_source": self._label_source,
            },
        )

    def _classifier_version(self) -> str:
        # When the label source changes the prediction space (section_gold can
        # emit COMPLIANCE_FILING) we bump the version so downstream consumers
        # can tell results apart.
        if self._label_source == LABEL_SOURCE_RULE_V1:
            return "v1"
        return "v2"

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_slice(self, slices: Any) -> str:
        kind = self._embedding_kind
        if kind == "full_text":
            return slices.full_text or ""
        elif kind == "first_3_pages":
            return slices.first_3_pages or ""
        elif kind == "title_block":
            return slices.title_block or ""
        elif kind == "rate_table_text":
            return slices.rate_table_text or ""
        elif kind == "order_conclusion_section":
            return slices.order_conclusion_section or ""
        return ""

    def _load_reference_vectors(self) -> None:
        """Load reference vectors from DB, caching by model + kind."""
        import sqlite3

        model = self._orchestrator._roles[self._model_role].primary
        key = (model, self._embedding_kind)
        if key == (self._loaded_model, self._loaded_kind):
            return

        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, source_pdf, vector
                FROM document_embeddings
                WHERE embedding_kind = ?
                  AND embedding_model = ?
                ORDER BY id
                """,
                (self._embedding_kind, model),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            self._ref_vectors = None
            self._ref_pdfs = []
            self._ref_ids = []
            return

        vectors: list[np.ndarray] = []
        pdfs: list[str] = []
        ids: list[int] = []
        for row_id, source_pdf, blob in rows:
            try:
                vec = np.frombuffer(blob, dtype=np.float32)
                if len(vec) > 0:
                    vectors.append(vec)
                    pdfs.append(source_pdf)
                    ids.append(row_id)
            except Exception:
                continue

        if vectors:
            self._ref_vectors = np.stack(vectors, axis=0)
        else:
            self._ref_vectors = None
        self._ref_pdfs = pdfs
        self._ref_ids = ids
        self._loaded_model = model
        self._loaded_kind = self._embedding_kind

    def _cosine_similarity(
        self, query: np.ndarray, refs: np.ndarray
    ) -> np.ndarray:
        """Compute cosine similarity between query and all reference vectors."""
        q_norm = query / (np.linalg.norm(query) + 1e-10)
        r_norms = refs / (np.linalg.norm(refs, axis=1, keepdims=True) + 1e-10)
        return r_norms @ q_norm

    def _lookup_labels(self, top_indices: np.ndarray) -> list[dict[str, Any]]:
        """Look up document_type labels for the given neighbor indices.

        Branches on ``label_source``:
        - ``rule_v1``: read rule_document_type_v1 from document_classifications.
        - ``section_gold``: derive from aggregated section_type_gold rows.
        - ``section_gold_or_rule``: prefer section_gold, fall back to rule_v1.
        """
        import sqlite3

        top_pdfs = [self._ref_pdfs[i] for i in top_indices]
        if not top_pdfs:
            return []

        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            section_map: dict[str, dict] = {}
            if self._label_source in (
                LABEL_SOURCE_SECTION_GOLD,
                LABEL_SOURCE_SECTION_GOLD_OR_RULE,
            ):
                section_map = fetch_section_derived_labels(conn)

            rule_map: dict[str, dict] = {}
            if self._label_source in (
                LABEL_SOURCE_RULE_V1,
                LABEL_SOURCE_SECTION_GOLD_OR_RULE,
            ):
                rule_map = self._fetch_rule_v1_labels(conn, top_pdfs)
        finally:
            conn.close()

        results: list[dict[str, Any]] = []
        for pdf in top_pdfs:
            if (
                self._label_source == LABEL_SOURCE_SECTION_GOLD_OR_RULE
                and pdf in section_map
            ):
                entry = section_map[pdf]
                results.append(
                    {
                        "label": entry["label"],
                        "confidence": entry["confidence"],
                        "subject_id": "",
                        "source_pdf": pdf,
                        "label_source": "section_gold",
                    }
                )
            elif self._label_source == LABEL_SOURCE_SECTION_GOLD:
                entry = section_map.get(pdf)
                if entry is None:
                    results.append(
                        {
                            "label": "UNKNOWN",
                            "confidence": 0.0,
                            "subject_id": "",
                            "source_pdf": pdf,
                            "label_source": "section_gold_missing",
                        }
                    )
                else:
                    results.append(
                        {
                            "label": entry["label"],
                            "confidence": entry["confidence"],
                            "subject_id": "",
                            "source_pdf": pdf,
                            "label_source": "section_gold",
                        }
                    )
            else:  # rule_v1, or section_gold_or_rule with no section coverage
                entry = rule_map.get(pdf)
                if entry is None:
                    results.append(
                        {
                            "label": "UNKNOWN",
                            "confidence": 0.0,
                            "subject_id": "",
                            "source_pdf": pdf,
                            "label_source": "rule_v1_missing",
                        }
                    )
                else:
                    results.append({**entry, "label_source": "rule_v1"})
        return results

    def _fetch_rule_v1_labels(
        self, conn: Any, top_pdfs: list[str]
    ) -> dict[str, dict]:
        placeholders = ",".join("?" for _ in top_pdfs)
        rows = conn.execute(
            f"""
            SELECT hd.id, hd.local_path, dc.label, dc.confidence
            FROM historical_documents hd
            JOIN document_classifications dc
              ON dc.subject_id = CAST(hd.id AS TEXT)
             AND dc.subject_kind = 'historical_document'
             AND dc.stage = 'document_type'
             AND dc.classifier = 'rule_document_type_v1'
             AND dc.superseded_by IS NULL
            WHERE hd.local_path IN ({placeholders})
            """,
            top_pdfs,
        ).fetchall()
        out: dict[str, dict] = {}
        for row in rows:
            out[row["local_path"]] = {
                "label": row["label"] or "UNKNOWN",
                "confidence": row["confidence"] or 0.5,
                "subject_id": str(row["id"]),
                "source_pdf": row["local_path"],
            }
        return out

    def _unknown_result(
        self, reason: str, **extra: Any
    ) -> ClassificationResult:
        return ClassificationResult(
            label="UNKNOWN",
            confidence=0.0,
            classifier="embedding_knn_v1",
            classifier_version="v1",
            evidence=[{"kind": reason, "value": 0, **(extra or {})}],
        )
