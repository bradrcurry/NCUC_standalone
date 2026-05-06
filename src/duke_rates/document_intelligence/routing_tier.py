"""
Routing tier system (Phase 2 of parsing-architecture refactor).

Labels every document with a routing tier (TIER 1 / 2 / 3) derived from its
``document_identity`` bundle. The tier label is **informational only** in
Phase 2 — it does not change extraction behavior. Phase 3 (template
binding) and Phase 4 (per-doc rules) consume the labels to make routing
decisions.

Tier definitions (per plan §5.2A):

- **TIER 1** — high-confidence identity, safe to bind to a profile template.
  Criteria: ``overall_confidence >= 0.85`` AND
  ``profile_consensus_margin >= 0.15``.
- **TIER 2** — uncertain identity. Try the top template; if extraction
  fails, fall back to the runner-up.
  Criteria: ``0.5 <= overall_confidence < 0.85`` OR margin below 0.15.
- **TIER 3** — low-confidence identity or no consensus at all. Send to the
  per-document-rules track in Phase 4.
  Criteria: ``overall_confidence < 0.5`` OR no profile consensus.

Plan reference: ``docs/PARSING_ARCHITECTURE_REFACTOR_PLAN.md`` §5.

Usage::

    from duke_rates.document_intelligence.routing_tier import (
        classify_tier, TierClassification, TierAggregator,
    )
    bundle = fetch_identity(db_path, source_pdf)
    tier = classify_tier(bundle)
    # ... or batch-label everything:
    agg = TierAggregator(db_path)
    agg.label_all()
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tier constants — keep these tunable via the quality reports in §5.2B/C
# ---------------------------------------------------------------------------

# A bundle must clear BOTH the confidence and margin gates to be Tier 1.
TIER_1_MIN_CONFIDENCE = 0.85
TIER_1_MIN_MARGIN = 0.15

# Tier 2 spans the uncertain middle.
TIER_2_MIN_CONFIDENCE = 0.5

# Anything below TIER_2_MIN_CONFIDENCE — or with no profile consensus at
# all — is Tier 3.


class Tier(IntEnum):
    TIER_1 = 1  # high-confidence, ready for template binding
    TIER_2 = 2  # uncertain — try template, fall back to runner-up
    TIER_3 = 3  # low-confidence — send to per-doc rules track


@dataclass
class TierClassification:
    """Pure-data result of classifying one identity bundle."""

    source_pdf: str
    tier: Tier
    overall_confidence: float
    profile_consensus_top: str | None
    profile_consensus_confidence: float | None
    profile_consensus_margin: float | None
    rationale: str  # one-line human-readable reason

    def to_persistence_tuple(self) -> tuple[Any, ...]:
        return (
            self.source_pdf,
            int(self.tier),
            self.overall_confidence,
            self.profile_consensus_top,
            self.profile_consensus_confidence,
            self.profile_consensus_margin,
            self.rationale,
        )


# ---------------------------------------------------------------------------
# Pure classification function
# ---------------------------------------------------------------------------


def classify_tier(identity_bundle: dict[str, Any]) -> TierClassification:
    """Assign a tier to one ``document_identity`` row.

    Accepts the row dict produced by
    :func:`document_identity.fetch_identity` (or the upsert tuple's
    column order). Pure function — no DB writes.

    Decision logic mirrors plan §5.2A:

    - Tier 1 if ``overall_confidence >= 0.85`` AND
      ``profile_consensus_margin >= 0.15``.
    - Tier 3 if ``overall_confidence < 0.5`` OR ``profile_consensus_top``
      is missing.
    - Tier 2 otherwise (uncertain middle, including margin-below-threshold).
    """
    source_pdf = identity_bundle.get("source_pdf") or ""
    confidence = float(identity_bundle.get("overall_confidence") or 0.0)
    consensus_top = identity_bundle.get("profile_consensus_top") or None
    consensus_conf = identity_bundle.get("profile_consensus_confidence")
    consensus_margin_raw = identity_bundle.get("profile_consensus_margin")
    consensus_margin = (
        float(consensus_margin_raw) if consensus_margin_raw is not None else None
    )

    # Tier 3 dominates: no consensus or low overall confidence.
    if not consensus_top:
        tier = Tier.TIER_3
        rationale = (
            f"no profile consensus (overall_confidence={confidence:.2f})"
        )
    elif confidence < TIER_2_MIN_CONFIDENCE:
        tier = Tier.TIER_3
        rationale = (
            f"overall_confidence {confidence:.2f} below tier-2 floor "
            f"{TIER_2_MIN_CONFIDENCE}"
        )
    elif (
        confidence >= TIER_1_MIN_CONFIDENCE
        and consensus_margin is not None
        and consensus_margin >= TIER_1_MIN_MARGIN
    ):
        tier = Tier.TIER_1
        rationale = (
            f"high confidence {confidence:.2f}, "
            f"consensus margin {consensus_margin:.2f}"
        )
    elif confidence >= TIER_1_MIN_CONFIDENCE:
        # High overall confidence but margin too thin — keep at Tier 2.
        tier = Tier.TIER_2
        margin_str = (
            f"{consensus_margin:.2f}" if consensus_margin is not None else "n/a"
        )
        rationale = (
            f"high confidence {confidence:.2f} but consensus margin "
            f"{margin_str} below {TIER_1_MIN_MARGIN}"
        )
    else:
        tier = Tier.TIER_2
        rationale = (
            f"mid confidence {confidence:.2f} with consensus={consensus_top!r}"
        )

    return TierClassification(
        source_pdf=source_pdf,
        tier=tier,
        overall_confidence=confidence,
        profile_consensus_top=consensus_top,
        profile_consensus_confidence=(
            float(consensus_conf) if consensus_conf is not None else None
        ),
        profile_consensus_margin=consensus_margin,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL_DOCUMENT_ROUTING_TIER = """
CREATE TABLE IF NOT EXISTS document_routing_tier (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_pdf TEXT NOT NULL UNIQUE,
    tier INTEGER NOT NULL,
    overall_confidence REAL NOT NULL,
    profile_consensus_top TEXT,
    profile_consensus_confidence REAL,
    profile_consensus_margin REAL,
    rationale TEXT NOT NULL DEFAULT '',
    last_updated TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_DDL_INDEX_TIER = (
    "CREATE INDEX IF NOT EXISTS idx_routing_tier_tier "
    "ON document_routing_tier(tier);"
)
_DDL_INDEX_PDF = (
    "CREATE INDEX IF NOT EXISTS idx_routing_tier_pdf "
    "ON document_routing_tier(source_pdf);"
)


def ensure_schema(db_path: Path | str) -> None:
    """Idempotent schema bootstrap."""
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute(_DDL_DOCUMENT_ROUTING_TIER)
        conn.execute(_DDL_INDEX_TIER)
        conn.execute(_DDL_INDEX_PDF)
        conn.commit()
        conn.close()
    except Exception:
        logger.warning("document_routing_tier schema bootstrap failed", exc_info=True)


# ---------------------------------------------------------------------------
# Aggregator (batch labeling)
# ---------------------------------------------------------------------------


class TierAggregator:
    """Batch-label every document in ``document_identity`` with a tier.

    Idempotent: ``label_all()`` upserts rows in ``document_routing_tier``,
    keyed by ``source_pdf``.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        ensure_schema(self._db_path)

    def label_all(self, *, limit: int | None = None) -> int:
        """Classify every identity row and upsert tier labels.

        Returns the number of rows processed.
        """
        n = 0
        for row in self._iter_identities(limit=limit):
            try:
                tc = classify_tier(row)
                self._upsert(tc)
                n += 1
            except Exception:
                logger.warning(
                    "tier classification failed for %s",
                    row.get("source_pdf"), exc_info=True,
                )
        return n

    def label_one(self, source_pdf: str) -> TierClassification | None:
        """Classify a single doc and upsert its tier.

        Returns the classification, or None if the doc has no identity bundle.
        """
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM document_identity WHERE source_pdf = ? LIMIT 1",
                (source_pdf,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        tc = classify_tier(dict(row))
        self._upsert(tc)
        return tc

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _iter_identities(self, *, limit: int | None = None):
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            sql = "SELECT * FROM document_identity"
            if limit:
                sql += f" LIMIT {int(limit)}"
            rows = conn.execute(sql).fetchall()
        finally:
            conn.close()
        for r in rows:
            yield dict(r)

    def _upsert(self, tc: TierClassification) -> None:
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.execute(
                """
                INSERT INTO document_routing_tier
                    (source_pdf, tier, overall_confidence,
                     profile_consensus_top, profile_consensus_confidence,
                     profile_consensus_margin, rationale, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(source_pdf) DO UPDATE SET
                    tier = excluded.tier,
                    overall_confidence = excluded.overall_confidence,
                    profile_consensus_top = excluded.profile_consensus_top,
                    profile_consensus_confidence = excluded.profile_consensus_confidence,
                    profile_consensus_margin = excluded.profile_consensus_margin,
                    rationale = excluded.rationale,
                    last_updated = excluded.last_updated
                """,
                tc.to_persistence_tuple(),
            )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Read API for downstream consumers (Phase 2B/2C, future Phase 3/4)
# ---------------------------------------------------------------------------


def fetch_tier(db_path: Path | str, source_pdf: str) -> dict[str, Any] | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM document_routing_tier WHERE source_pdf = ? LIMIT 1",
            (source_pdf,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def fetch_tier_distribution(db_path: Path | str) -> dict[int, int]:
    """Tier -> count map for the dashboard."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT tier, COUNT(*) FROM document_routing_tier GROUP BY tier"
        ).fetchall()
    finally:
        conn.close()
    return {int(r[0]): int(r[1]) for r in rows}


# ---------------------------------------------------------------------------
# Phase 2B — tier-prediction validation
# ---------------------------------------------------------------------------


def build_tier_validation_report(db_path: Path | str) -> dict[str, Any]:
    """Cross-check predicted tier against actual parse outcome.

    Returns a structured report (suitable for JSON serialization) with:

    - **tier_outcomes** — for each tier, count of attempts by status
      (parsed, empty, failed, partial, ...) and parsed-with-charges rate.
    - **tier_diagnoses** — for each tier, count of failure_type from
      ``llm_parse_diagnostics`` (so we can see whether Tier 3 docs really
      do correlate with ``wrong_profile`` / ``unknown``).
    - **tier1_extraction_failures** — Tier 1 docs whose parse_attempt_logs
      status is NOT ``parsed`` (these are TEMPLATE bugs per plan §5.2B).
    - **tier3_unexpected_successes** — Tier 3 docs that parsed cleanly
      anyway (cutoffs may be too strict; sample to inspect).
    - **summary** — one-line counts for the dashboard header.

    Does NOT modify any data. Pure aggregation query. Plan §5.2B.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # Tier x parse_attempt status crosstab.
        outcome_rows = conn.execute(
            """
            SELECT rt.tier,
                   COALESCE(pal.status, 'no_attempt') AS status,
                   COUNT(*) AS cnt,
                   SUM(CASE WHEN pal.charge_count > 0 THEN 1 ELSE 0 END) AS with_charges
            FROM document_routing_tier rt
            LEFT JOIN parse_attempt_logs pal ON pal.source_pdf = rt.source_pdf
            GROUP BY rt.tier, COALESCE(pal.status, 'no_attempt')
            """
        ).fetchall()
        # Tier x diagnosed failure_type crosstab.
        diag_rows = conn.execute(
            """
            SELECT rt.tier,
                   ld.failure_type,
                   COUNT(*) AS cnt
            FROM document_routing_tier rt
            JOIN parse_attempt_logs pal ON pal.source_pdf = rt.source_pdf
            JOIN llm_parse_diagnostics ld ON ld.parse_attempt_id = pal.id
            GROUP BY rt.tier, ld.failure_type
            """
        ).fetchall()
        # Tier 1 extraction failures (template bugs per plan §5.2B).
        tier1_failures = conn.execute(
            """
            SELECT rt.source_pdf,
                   rt.profile_consensus_top,
                   pal.parser_profile,
                   pal.status,
                   pal.charge_count,
                   ld.failure_type
            FROM document_routing_tier rt
            JOIN parse_attempt_logs pal ON pal.source_pdf = rt.source_pdf
            LEFT JOIN llm_parse_diagnostics ld ON ld.parse_attempt_id = pal.id
            WHERE rt.tier = 1
              AND (pal.status != 'parsed' OR COALESCE(pal.charge_count, 0) = 0)
            ORDER BY rt.overall_confidence DESC
            LIMIT 50
            """
        ).fetchall()
        # Tier 3 unexpected successes (cutoffs may be too strict).
        tier3_successes = conn.execute(
            """
            SELECT rt.source_pdf,
                   rt.overall_confidence,
                   rt.profile_consensus_top,
                   pal.parser_profile,
                   pal.charge_count
            FROM document_routing_tier rt
            JOIN parse_attempt_logs pal ON pal.source_pdf = rt.source_pdf
            WHERE rt.tier = 3
              AND pal.status = 'parsed'
              AND pal.charge_count > 0
            ORDER BY rt.overall_confidence DESC
            LIMIT 50
            """
        ).fetchall()
    finally:
        conn.close()

    # Pivot outcome rows into nested dict per tier.
    tier_outcomes: dict[int, dict[str, Any]] = {}
    for r in outcome_rows:
        tier = int(r["tier"])
        bucket = tier_outcomes.setdefault(
            tier, {"by_status": {}, "total": 0, "parsed_with_charges": 0}
        )
        bucket["by_status"][r["status"]] = int(r["cnt"])
        bucket["total"] += int(r["cnt"])
        bucket["parsed_with_charges"] += int(r["with_charges"] or 0)
    for bucket in tier_outcomes.values():
        total = bucket["total"]
        bucket["parsed_with_charges_rate"] = (
            round(bucket["parsed_with_charges"] / total, 3) if total else 0.0
        )

    tier_diagnoses: dict[int, dict[str, int]] = {}
    for r in diag_rows:
        tier_diagnoses.setdefault(int(r["tier"]), {})[
            r["failure_type"]
        ] = int(r["cnt"])

    summary: dict[str, Any] = {
        "tier1_count": tier_outcomes.get(1, {}).get("total", 0),
        "tier2_count": tier_outcomes.get(2, {}).get("total", 0),
        "tier3_count": tier_outcomes.get(3, {}).get("total", 0),
        "tier1_parsed_rate": tier_outcomes.get(1, {}).get(
            "parsed_with_charges_rate", 0.0
        ),
        "tier3_parsed_rate": tier_outcomes.get(3, {}).get(
            "parsed_with_charges_rate", 0.0
        ),
        "tier1_extraction_failure_count": len(tier1_failures),
        "tier3_unexpected_success_count": len(tier3_successes),
    }

    return {
        "tier_outcomes": tier_outcomes,
        "tier_diagnoses": tier_diagnoses,
        "tier1_extraction_failures": [dict(r) for r in tier1_failures],
        "tier3_unexpected_successes": [dict(r) for r in tier3_successes],
        "summary": summary,
    }
