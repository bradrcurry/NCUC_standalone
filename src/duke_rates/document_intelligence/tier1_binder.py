"""
Tier 1 binder (Phase 3B of parsing-architecture refactor).

For each Tier 1 doc — high-confidence identity, consensus profile margin
above threshold — record a binding decision: which profile/template
should handle this doc, whether it's safe to bind (per the
anchor-required scope check), and what the resulting parse outcome was
(if active-mode is on).

Plan reference: ``docs/PARSING_ARCHITECTURE_REFACTOR_PLAN.md`` §6.3B.

This module is **observability-first**:
- ``dry_run=True`` (default): compute the binding decision, persist a
  proposal row, do NOT alter parse_attempt_logs. Phase 3C consumes the
  proposals to compare against current routing.
- ``dry_run=False`` (active mode, future): the binder may invoke the
  existing parser pipeline for the proposed profile. NOT enabled in
  this implementation — Phase 3B ships only the proposal recorder so
  that 3C's comparison runs cleanly first.

Tier 1 docs that bind to an anchor-required profile or to ``unknown``
are flagged ``refused`` rather than ``proposed``; the rationale is
recorded so future agents can debug why a high-confidence doc didn't
get bound.

Usage::

    from duke_rates.document_intelligence.tier1_binder import (
        Tier1Binder, ensure_schema,
    )
    binder = Tier1Binder(db_path)
    binder.bind_all()             # dry-run: records proposals
    # ... or single-doc:
    binder.bind_one(source_pdf)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from duke_rates.document_intelligence.profile_template_metadata import (
    get_template_metadata,
    is_safe_for_tier1_binding,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

# A binding proposal can be in one of these states.
STATUS_PROPOSED = "proposed"          # Tier 1 + safe template + no extraction yet
STATUS_REFUSED = "refused"            # Tier 1 but template flagged unsafe
STATUS_APPLIED = "applied"            # active mode: parser ran, charges extracted
STATUS_TEMPLATE_BUG = "template_bug"  # active mode: parser ran, no charges -> template fix needed
STATUS_NO_CONSENSUS = "no_consensus"  # Tier 1 row but consensus_top is empty (data bug)

ALL_STATUSES: tuple[str, ...] = (
    STATUS_PROPOSED, STATUS_REFUSED, STATUS_APPLIED,
    STATUS_TEMPLATE_BUG, STATUS_NO_CONSENSUS,
)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL_TIER1_BINDING = """
CREATE TABLE IF NOT EXISTS tier1_binding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_pdf TEXT NOT NULL UNIQUE,
    proposed_profile TEXT NOT NULL,
    consensus_confidence REAL,
    consensus_margin REAL,
    overall_confidence REAL,
    current_parser_profile TEXT,         -- whatever parse_attempt_logs has now (informational)
    agreement_with_current INTEGER,      -- 1 if proposed == current, 0 otherwise, NULL if no current
    template_scope TEXT,                 -- snapshot of catalog scope at decision time
    status TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    last_updated TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_DDL_INDEX_STATUS = (
    "CREATE INDEX IF NOT EXISTS idx_tier1_binding_status "
    "ON tier1_binding(status);"
)
_DDL_INDEX_PROFILE = (
    "CREATE INDEX IF NOT EXISTS idx_tier1_binding_profile "
    "ON tier1_binding(proposed_profile);"
)


def ensure_schema(db_path: Path | str) -> None:
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute(_DDL_TIER1_BINDING)
        conn.execute(_DDL_INDEX_STATUS)
        conn.execute(_DDL_INDEX_PROFILE)
        conn.commit()
        conn.close()
    except Exception:
        logger.warning("tier1_binding schema bootstrap failed", exc_info=True)


# ---------------------------------------------------------------------------
# Decision result
# ---------------------------------------------------------------------------


@dataclass
class BindingDecision:
    source_pdf: str
    proposed_profile: str
    consensus_confidence: float | None
    consensus_margin: float | None
    overall_confidence: float | None
    current_parser_profile: str | None
    agreement_with_current: bool | None  # None when no current parse exists
    template_scope: str | None
    status: str
    rationale: str

    def to_persistence_tuple(self) -> tuple[Any, ...]:
        agreement_int: int | None
        if self.agreement_with_current is None:
            agreement_int = None
        else:
            agreement_int = 1 if self.agreement_with_current else 0
        return (
            self.source_pdf,
            self.proposed_profile,
            self.consensus_confidence,
            self.consensus_margin,
            self.overall_confidence,
            self.current_parser_profile,
            agreement_int,
            self.template_scope,
            self.status,
            self.rationale,
        )


# ---------------------------------------------------------------------------
# Binder
# ---------------------------------------------------------------------------


class Tier1Binder:
    """Records Tier 1 binding decisions.

    Phase 3B is observability-only — proposals are persisted but no
    extraction is performed. Phase 3C consumes these proposals to
    compare against current routing before active mode is flipped on.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        ensure_schema(self._db_path)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def bind_all(self, *, limit: int | None = None) -> dict[str, int]:
        """Record proposals for every Tier 1 doc.

        Returns a status -> count summary.
        """
        counts: dict[str, int] = {s: 0 for s in ALL_STATUSES}
        for row in self._iter_tier1_rows(limit=limit):
            try:
                d = self.decide(row)
                self._upsert(d)
                counts[d.status] = counts.get(d.status, 0) + 1
            except Exception:
                logger.warning(
                    "tier1 binding failed for %s",
                    row.get("source_pdf"), exc_info=True,
                )
        return counts

    def bind_one(self, source_pdf: str) -> BindingDecision | None:
        """Record a proposal for a single doc; returns the decision."""
        row = self._fetch_tier1_row(source_pdf)
        if row is None:
            return None
        d = self.decide(row)
        self._upsert(d)
        return d

    def decide(self, tier_row: dict[str, Any]) -> BindingDecision:
        """Pure decision function — no DB writes.

        Accepts a row dict from ``document_routing_tier`` (joined with the
        current parse_attempt_logs.parser_profile if available) and returns
        a ``BindingDecision``.
        """
        source_pdf = tier_row.get("source_pdf") or ""
        consensus_top = tier_row.get("profile_consensus_top") or ""
        consensus_conf = tier_row.get("profile_consensus_confidence")
        consensus_margin = tier_row.get("profile_consensus_margin")
        overall_conf = tier_row.get("overall_confidence")
        current_profile = tier_row.get("current_parser_profile")

        # No consensus at all → data bug; should be impossible for Tier 1
        # (the classifier would have put it in Tier 3) but guard anyway.
        if not consensus_top:
            return BindingDecision(
                source_pdf=source_pdf,
                proposed_profile="",
                consensus_confidence=consensus_conf,
                consensus_margin=consensus_margin,
                overall_confidence=overall_conf,
                current_parser_profile=current_profile,
                agreement_with_current=None,
                template_scope=None,
                status=STATUS_NO_CONSENSUS,
                rationale="Tier 1 row has empty profile_consensus_top",
            )

        md = get_template_metadata(consensus_top)
        scope = md.scope if md else None
        safe, reason = is_safe_for_tier1_binding(consensus_top)

        agreement: bool | None
        if current_profile is None or current_profile == "":
            agreement = None
        else:
            agreement = current_profile == consensus_top

        if not safe:
            return BindingDecision(
                source_pdf=source_pdf,
                proposed_profile=consensus_top,
                consensus_confidence=consensus_conf,
                consensus_margin=consensus_margin,
                overall_confidence=overall_conf,
                current_parser_profile=current_profile,
                agreement_with_current=agreement,
                template_scope=scope,
                status=STATUS_REFUSED,
                rationale=reason,
            )

        return BindingDecision(
            source_pdf=source_pdf,
            proposed_profile=consensus_top,
            consensus_confidence=consensus_conf,
            consensus_margin=consensus_margin,
            overall_confidence=overall_conf,
            current_parser_profile=current_profile,
            agreement_with_current=agreement,
            template_scope=scope,
            status=STATUS_PROPOSED,
            rationale=(
                f"Tier 1, consensus={consensus_top}, "
                f"margin={consensus_margin}, "
                f"current_parser_profile={current_profile!r}"
            ),
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _iter_tier1_rows(self, *, limit: int | None = None):
        """Stream Tier 1 docs joined with their current parser_profile.

        ``current_parser_profile`` is taken from the most-recent
        parse_attempt_logs row for the source_pdf; we accept that some docs
        have multiple attempts and pick the latest by id.
        """
        sql = """
        SELECT rt.source_pdf,
               rt.profile_consensus_top,
               rt.profile_consensus_confidence,
               rt.profile_consensus_margin,
               rt.overall_confidence,
               (
                 SELECT pal.parser_profile
                 FROM parse_attempt_logs pal
                 WHERE pal.source_pdf = rt.source_pdf
                 ORDER BY pal.id DESC LIMIT 1
               ) AS current_parser_profile
        FROM document_routing_tier rt
        WHERE rt.tier = 1
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(sql).fetchall()
        finally:
            conn.close()
        for r in rows:
            yield dict(r)

    def _fetch_tier1_row(self, source_pdf: str) -> dict[str, Any] | None:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT rt.source_pdf,
                       rt.profile_consensus_top,
                       rt.profile_consensus_confidence,
                       rt.profile_consensus_margin,
                       rt.overall_confidence,
                       (
                         SELECT pal.parser_profile
                         FROM parse_attempt_logs pal
                         WHERE pal.source_pdf = rt.source_pdf
                         ORDER BY pal.id DESC LIMIT 1
                       ) AS current_parser_profile
                FROM document_routing_tier rt
                WHERE rt.tier = 1 AND rt.source_pdf = ?
                LIMIT 1
                """,
                (source_pdf,),
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def _upsert(self, d: BindingDecision) -> None:
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.execute(
                """
                INSERT INTO tier1_binding
                    (source_pdf, proposed_profile, consensus_confidence,
                     consensus_margin, overall_confidence,
                     current_parser_profile, agreement_with_current,
                     template_scope, status, rationale, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(source_pdf) DO UPDATE SET
                    proposed_profile = excluded.proposed_profile,
                    consensus_confidence = excluded.consensus_confidence,
                    consensus_margin = excluded.consensus_margin,
                    overall_confidence = excluded.overall_confidence,
                    current_parser_profile = excluded.current_parser_profile,
                    agreement_with_current = excluded.agreement_with_current,
                    template_scope = excluded.template_scope,
                    status = excluded.status,
                    rationale = excluded.rationale,
                    last_updated = excluded.last_updated
                """,
                d.to_persistence_tuple(),
            )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Read API for downstream consumers (Phase 3C, future Phase 4)
# ---------------------------------------------------------------------------


def fetch_binding_summary(db_path: Path | str) -> dict[str, Any]:
    """Aggregate counts for the dashboard.

    Returns a dict with:
      - status_counts: status -> count
      - by_profile: proposed_profile -> count (top 20)
      - agreement: counts of agreement_with_current values (yes/no/none)
    """
    conn = sqlite3.connect(str(db_path))
    try:
        status_rows = conn.execute(
            "SELECT status, COUNT(*) FROM tier1_binding GROUP BY status ORDER BY 2 DESC"
        ).fetchall()
        profile_rows = conn.execute(
            """
            SELECT proposed_profile, COUNT(*)
            FROM tier1_binding
            WHERE status IN (?, ?)
            GROUP BY proposed_profile
            ORDER BY 2 DESC LIMIT 20
            """,
            (STATUS_PROPOSED, STATUS_APPLIED),
        ).fetchall()
        agreement_rows = conn.execute(
            """
            SELECT
                CASE
                    WHEN agreement_with_current IS NULL THEN 'no_current'
                    WHEN agreement_with_current = 1 THEN 'agree'
                    ELSE 'disagree'
                END AS bucket,
                COUNT(*) AS cnt
            FROM tier1_binding
            GROUP BY 1
            """
        ).fetchall()
        disagree_rows = conn.execute(
            """
            SELECT source_pdf, current_parser_profile, proposed_profile,
                   overall_confidence, status
            FROM tier1_binding
            WHERE agreement_with_current = 0
              AND status = ?
            ORDER BY overall_confidence DESC
            LIMIT 50
            """,
            (STATUS_PROPOSED,),
        ).fetchall()
    finally:
        conn.close()
    return {
        "status_counts": {r[0]: int(r[1]) for r in status_rows},
        "by_profile": {r[0]: int(r[1]) for r in profile_rows},
        "agreement": {r[0]: int(r[1]) for r in agreement_rows},
        "disagreement_samples": [
            {
                "source_pdf": r[0],
                "current_parser_profile": r[1],
                "proposed_profile": r[2],
                "overall_confidence": r[3],
                "status": r[4],
            }
            for r in disagree_rows
        ],
    }


# ---------------------------------------------------------------------------
# Phase 3C — comparison report
# ---------------------------------------------------------------------------

# Disagreement categories — these tell us *why* the binder differs from
# current routing. Each maps to a different remediation path.
DISAGREE_CURRENT_UNKNOWN = "current_unknown"           # legacy classifier missed it
DISAGREE_CURRENT_OTHER_TEMPLATE = "current_other"      # both ran a template, but different
DISAGREE_NO_CURRENT_ATTEMPT = "no_current_attempt"     # never parsed before


def build_comparison_report(db_path: Path | str) -> dict[str, Any]:
    """Compare Tier 1 binder proposals against current parser_profile.

    Implements plan §6.3C: bucket disagreements by reason, surface the
    biggest disagreement types, and compute a disagreement rate.

    Returns a dict with:
      - totals: tier1 docs, proposals, refusals, no_consensus
      - agreement_rate / disagreement_rate (0.0 – 1.0)
      - disagreements_by_kind: kind -> count
      - top_flips: (current_profile, proposed_profile) -> count, top 20
      - parsed_outcome_when_disagree: how many disagreement docs are
        currently parsed vs empty/failed — tells us which side is "right"
      - sample_rows: up to 20 disagreement examples for the dashboard
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT tb.source_pdf,
                   tb.proposed_profile,
                   tb.current_parser_profile,
                   tb.agreement_with_current,
                   tb.overall_confidence,
                   tb.status,
                   (
                     SELECT pal.status
                     FROM parse_attempt_logs pal
                     WHERE pal.source_pdf = tb.source_pdf
                     ORDER BY pal.id DESC LIMIT 1
                   ) AS current_parse_status,
                   (
                     SELECT pal.charge_count
                     FROM parse_attempt_logs pal
                     WHERE pal.source_pdf = tb.source_pdf
                     ORDER BY pal.id DESC LIMIT 1
                   ) AS current_charge_count
            FROM tier1_binding tb
            """
        ).fetchall()
    finally:
        conn.close()

    totals = {
        "tier1_total": len(rows),
        "proposed": 0,
        "refused": 0,
        "no_consensus": 0,
        "applied": 0,
        "template_bug": 0,
    }
    agree = 0
    disagree = 0
    no_current = 0
    disagreements_by_kind: dict[str, int] = {}
    flip_counts: dict[tuple[str, str], int] = {}
    parsed_outcome_when_disagree = {"parsed_with_charges": 0, "empty_or_failed": 0, "other": 0}
    sample_rows: list[dict[str, Any]] = []

    for r in rows:
        status = r["status"]
        totals[status] = totals.get(status, 0) + 1

        # Only proposals participate in agreement math; refusals/no_consensus
        # didn't produce a binding to compare with.
        if status != STATUS_PROPOSED:
            continue
        agreement = r["agreement_with_current"]
        if agreement is None:
            no_current += 1
            kind = DISAGREE_NO_CURRENT_ATTEMPT
            disagreements_by_kind[kind] = disagreements_by_kind.get(kind, 0) + 1
            continue
        if agreement == 1:
            agree += 1
            continue
        # Disagreement.
        disagree += 1
        current = r["current_parser_profile"] or ""
        proposed = r["proposed_profile"] or ""
        if current == "unknown":
            kind = DISAGREE_CURRENT_UNKNOWN
        else:
            kind = DISAGREE_CURRENT_OTHER_TEMPLATE
        disagreements_by_kind[kind] = disagreements_by_kind.get(kind, 0) + 1
        flip_key = (current, proposed)
        flip_counts[flip_key] = flip_counts.get(flip_key, 0) + 1

        cps = r["current_parse_status"]
        ccc = r["current_charge_count"] or 0
        if cps == "parsed" and ccc > 0:
            parsed_outcome_when_disagree["parsed_with_charges"] += 1
        elif cps in ("empty", "failed", "partial"):
            parsed_outcome_when_disagree["empty_or_failed"] += 1
        else:
            parsed_outcome_when_disagree["other"] += 1

        if len(sample_rows) < 20:
            sample_rows.append({
                "source_pdf": r["source_pdf"],
                "current_parser_profile": current,
                "proposed_profile": proposed,
                "kind": kind,
                "overall_confidence": r["overall_confidence"],
                "current_parse_status": cps,
                "current_charge_count": ccc,
            })

    proposal_pool = agree + disagree + no_current
    agreement_rate = (agree / proposal_pool) if proposal_pool else 0.0
    disagreement_rate = (disagree / proposal_pool) if proposal_pool else 0.0

    top_flips = sorted(flip_counts.items(), key=lambda kv: -kv[1])[:20]

    return {
        "totals": totals,
        "agreement_count": agree,
        "disagreement_count": disagree,
        "no_current_count": no_current,
        "agreement_rate": round(agreement_rate, 3),
        "disagreement_rate": round(disagreement_rate, 3),
        "disagreements_by_kind": disagreements_by_kind,
        "top_flips": [
            {"current": k[0], "proposed": k[1], "count": v}
            for k, v in top_flips
        ],
        "parsed_outcome_when_disagree": parsed_outcome_when_disagree,
        "sample_rows": sample_rows,
    }
