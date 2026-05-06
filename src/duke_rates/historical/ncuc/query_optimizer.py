"""
Stage 5: Query performance tracker and optimizer.

Maintains per-query performance statistics across runs.  After each search
session, update scores with ``record_query_result()``.  Future runs can
call ``get_prioritized_queries()`` to get queries ordered by historical
usefulness.

Persistence is via a simple JSON file (data/manifests/query_optimizer.json).
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

from duke_rates.config import Settings
from duke_rates.historical.ncuc.query_builder import QuerySpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class QueryStats:
    """Accumulated performance statistics for a single query text."""
    query_text: str
    template_name: str
    utility_hint: str | None
    doc_type_hint: str | None

    # Lifetime counters
    run_count: int = 0
    syntax_error_count: int = 0
    total_results: int = 0
    total_high_score_results: int = 0   # results with local score > HIGH_SCORE_THRESHOLD
    total_noisy_results: int = 0        # results with negative ideality signals
    total_new_families: int = 0         # new document families discovered

    # Derived score (recomputed on load or update)
    usefulness_score: float = 0.0

    # Timestamps
    first_run_at: str | None = None
    last_run_at: str | None = None

    # Metadata
    retired: bool = False               # True if permanently demoted
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "QueryStats":
        d2 = dict(d)
        d2.setdefault("notes", [])
        d2.setdefault("retired", False)
        return cls(**d2)


# ---------------------------------------------------------------------------
# Scoring constants
# ---------------------------------------------------------------------------

HIGH_SCORE_THRESHOLD = 0.55     # local_score above which a result is "high quality"
MAX_USEFUL_SCORE = 10.0         # clamp ceiling
RETIRE_THRESHOLD = 0.05         # usefulness below which a query is retired
SYNTAX_ERROR_PENALTY = 3.0      # per error, subtracted from usefulness
MIN_RESULTS_FOR_ESTIMATION = 3  # need at least this many runs to trust the score


def _compute_usefulness(stats: QueryStats) -> float:
    """
    Score = weighted average quality of results, penalized for errors.

    Formula:
        base   = (high_score_results - 0.5 * noisy_results + 2 * new_families) / max(1, total_results)
        bonus  = log1p(total_high_score_results) * 0.5
        penalt = syntax_error_count * SYNTAX_ERROR_PENALTY / max(1, run_count)
        score  = base * run_weight + bonus - penalty
    """
    if stats.run_count == 0:
        return 0.0

    total = max(1, stats.total_results)
    quality_ratio = (
        stats.total_high_score_results
        - 0.5 * stats.total_noisy_results
        + 2.0 * stats.total_new_families
    ) / total

    bonus = math.log1p(stats.total_high_score_results) * 0.5

    error_rate = stats.syntax_error_count / stats.run_count
    penalty = error_rate * SYNTAX_ERROR_PENALTY

    # Dampen score for queries with very few runs
    run_weight = min(1.0, stats.run_count / MIN_RESULTS_FOR_ESTIMATION)

    score = quality_ratio * run_weight + bonus - penalty
    return max(0.0, min(MAX_USEFUL_SCORE, score))


# ---------------------------------------------------------------------------
# The optimizer
# ---------------------------------------------------------------------------

class QueryOptimizer:
    """
    Tracks per-query performance and emits a prioritized query list.

    Usage:
        optimizer = QueryOptimizer(settings)
        optimizer.load()

        # After a search session:
        optimizer.record_query_result(
            query_text="Duke Energy Progress tariff",
            template_name="utility_x_doctype",
            utility_hint="Duke Energy Progress",
            doc_type_hint="tariff",
            result_count=15,
            high_score_count=8,
            noisy_count=2,
            new_families=3,
            syntax_error=False,
        )
        optimizer.save()

        # Before next run:
        prioritized = optimizer.get_prioritized_queries(query_specs)
    """

    def __init__(self, settings: Settings, persist_path: Path | None = None):
        self.settings = settings
        self.persist_path = persist_path or (
            Path(settings.data_dir) / "manifests" / "query_optimizer.json"
        )
        self._stats: dict[str, QueryStats] = {}  # keyed by normalized query_text

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        if not self.persist_path.exists():
            return
        try:
            data = json.loads(self.persist_path.read_text(encoding="utf-8"))
            self._stats = {k: QueryStats.from_dict(v) for k, v in data.items()}
            logger.info("Loaded query optimizer: %d entries", len(self._stats))
        except Exception as exc:
            logger.warning("Failed to load query optimizer state: %s", exc)

    def save(self) -> None:
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v.to_dict() for k, v in self._stats.items()}
        self.persist_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info("Query optimizer saved: %d entries → %s", len(data), self.persist_path)

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    @staticmethod
    def _key(query_text: str) -> str:
        return query_text.strip().lower()

    def record_query_result(
        self,
        *,
        query_text: str,
        template_name: str,
        utility_hint: str | None = None,
        doc_type_hint: str | None = None,
        result_count: int = 0,
        high_score_count: int = 0,
        noisy_count: int = 0,
        new_families: int = 0,
        syntax_error: bool = False,
        notes: list[str] | None = None,
    ) -> QueryStats:
        """Record the outcome of one search query execution."""
        key = self._key(query_text)
        now = datetime.utcnow().isoformat()

        if key not in self._stats:
            self._stats[key] = QueryStats(
                query_text=query_text,
                template_name=template_name,
                utility_hint=utility_hint,
                doc_type_hint=doc_type_hint,
                first_run_at=now,
            )

        stats = self._stats[key]
        stats.run_count += 1
        stats.last_run_at = now
        stats.total_results += result_count
        stats.total_high_score_results += high_score_count
        stats.total_noisy_results += noisy_count
        stats.total_new_families += new_families
        if syntax_error:
            stats.syntax_error_count += 1
        if notes:
            stats.notes.extend(notes)

        # Recompute usefulness
        stats.usefulness_score = _compute_usefulness(stats)

        # Auto-retire queries with persistent errors and low usefulness
        if stats.syntax_error_count >= 3 and stats.usefulness_score < RETIRE_THRESHOLD:
            if not stats.retired:
                logger.info("Retiring low-performing query: %r (score=%.3f)", query_text, stats.usefulness_score)
                stats.retired = True
                stats.notes.append(f"auto_retired_at={now}")

        return stats

    def retire_query(self, query_text: str) -> None:
        """Manually retire a query."""
        key = self._key(query_text)
        if key in self._stats:
            self._stats[key].retired = True
            self._stats[key].notes.append(f"manually_retired_at={datetime.utcnow().isoformat()}")

    # ------------------------------------------------------------------
    # Prioritization
    # ------------------------------------------------------------------

    def get_prioritized_queries(
        self,
        query_specs: Iterable[QuerySpec],
        *,
        exclude_retired: bool = True,
        max_results: int | None = None,
    ) -> list[QuerySpec]:
        """
        Sort query_specs by a combined priority score:
            combined = spec.priority * (1 + usefulness_score)
        Unknown queries (no history) keep their spec.priority as-is.
        Retired queries are optionally excluded.
        """
        scored: list[tuple[float, QuerySpec]] = []
        for qs in query_specs:
            key = self._key(qs.query_text)
            stats = self._stats.get(key)

            if stats and stats.retired and exclude_retired:
                continue

            usefulness = stats.usefulness_score if stats else 0.0
            combined = qs.priority * (1.0 + usefulness)
            scored.append((combined, qs))

        scored.sort(key=lambda x: -x[0])
        result = [qs for _, qs in scored]
        if max_results:
            result = result[:max_results]
        return result

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def get_top_queries(self, n: int = 20) -> list[QueryStats]:
        """Return top N queries by usefulness score."""
        active = [s for s in self._stats.values() if not s.retired]
        return sorted(active, key=lambda s: -s.usefulness_score)[:n]

    def get_worst_queries(self, n: int = 10) -> list[QueryStats]:
        """Return worst N queries by usefulness score (for retirement candidates)."""
        active = [s for s in self._stats.values() if not s.retired and s.run_count >= 2]
        return sorted(active, key=lambda s: s.usefulness_score)[:n]

    def print_report(self, n: int = 20) -> None:
        """Print a query performance report."""
        all_stats = sorted(self._stats.values(), key=lambda s: -s.usefulness_score)
        active = [s for s in all_stats if not s.retired]
        retired = [s for s in all_stats if s.retired]

        print(f"\n=== Query Optimizer Report ({len(self._stats)} entries) ===")
        print(f"Active: {len(active)}   Retired: {len(retired)}")
        print()
        print(f"{'Query':<55} {'Runs':>5} {'Total':>6} {'High':>5} {'Noisy':>5} {'Fam':>4} {'Score':>6}")
        print("-" * 90)
        for s in active[:n]:
            q = s.query_text[:52] + "..." if len(s.query_text) > 55 else s.query_text
            print(
                f"{q:<55} {s.run_count:>5} {s.total_results:>6} "
                f"{s.total_high_score_results:>5} {s.total_noisy_results:>5} "
                f"{s.total_new_families:>4} {s.usefulness_score:>6.2f}"
            )

    def suggest_refinement_terms(self, top_n: int = 15) -> list[str]:
        """
        Analyze top-performing queries for shared terms that could seed
        refinement queries. Returns a de-duplicated list of useful terms
        not already in the base vocabulary.
        """
        from duke_rates.historical.ncuc.query_builder import (
            UTILITY_TERMS, DOCUMENT_TERMS, FINALITY_TERMS,
        )
        base_vocab = set()
        for term_list in [UTILITY_TERMS, DOCUMENT_TERMS, FINALITY_TERMS]:
            for t in term_list:
                base_vocab.add(t.lower())

        top_queries = self.get_top_queries(top_n)
        term_freq: dict[str, int] = {}
        for stats in top_queries:
            words = stats.query_text.lower().split()
            for w in words:
                w = w.strip('"\'()')
                if len(w) >= 4 and w not in base_vocab:
                    term_freq[w] = term_freq.get(w, 0) + 1

        # Return terms that appear in at least 2 top queries
        candidates = sorted(
            [(freq, term) for term, freq in term_freq.items() if freq >= 2],
            reverse=True,
        )
        return [term for _, term in candidates[:20]]
