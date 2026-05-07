"""
Promotion-path detection for per-document rules (Phase 4C of refactor).

Scans accepted ``document_specific_rules`` for clusters of regexes that
appear in N or more documents in the same family / same consensus
profile. When a cluster is detected, records a promotion candidate in
``template_promotion_candidates`` for human review.

This module does NOT auto-apply promotions to ``profile_templates.yaml``
or to the parser code — per the plan, a human (or a higher-confidence
LLM call with full context) approves before a per-doc rule becomes a
template-level rule. 4C ships the *detection*; the actual lift is a
separate manual or future-LLM step.

Plan reference: ``docs/PARSING_ARCHITECTURE_REFACTOR_PLAN.md`` §7.4C.

Algorithm:
    1. Pull all accepted rules with their parent document_identity row
       (so we have profile_consensus_top + identity signals).
    2. Group by (consensus_top, target_field). Within each group, cluster
       rules by regex similarity (cheap normalized-token Jaccard).
    3. A cluster of >= MIN_CLUSTER_SIZE rules is a promotion candidate.
       Pick the most-prevalent regex variant in the cluster as the
       suggested template-level pattern.
    4. Upsert a row in ``template_promotion_candidates`` (one per cluster).

Idempotent — running it twice doesn't duplicate candidates.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Plan §7.4C — N=3 initially.
MIN_CLUSTER_SIZE = 3

# Jaccard threshold for "string-similar" regexes. Above this, two regexes
# are considered the same pattern variant. Tuned conservatively because
# token-set Jaccard is forgiving on regex syntax.
SIMILARITY_THRESHOLD = 0.6


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL_CANDIDATES = """
CREATE TABLE IF NOT EXISTS template_promotion_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Group key: a stable id derived from (consensus_top, target_field, fingerprint)
    cluster_key TEXT NOT NULL UNIQUE,
    target_template TEXT NOT NULL,           -- which profile/template to promote into
    target_field TEXT,                       -- which field the cluster targets
    suggested_regex TEXT NOT NULL,           -- the most-prevalent variant in the cluster
    suggested_normalization TEXT,
    cluster_size INTEGER NOT NULL,           -- how many per-doc rules agreed
    member_rule_ids_json TEXT NOT NULL,      -- list of document_specific_rules.id
    sample_source_pdfs_json TEXT NOT NULL,   -- up to 5 PDFs for inspection
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | applied
    notes TEXT NOT NULL DEFAULT '',
    last_updated TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_DDL_INDEX_TEMPLATE = (
    "CREATE INDEX IF NOT EXISTS idx_promotion_candidates_template "
    "ON template_promotion_candidates(target_template);"
)
_DDL_INDEX_STATUS = (
    "CREATE INDEX IF NOT EXISTS idx_promotion_candidates_status "
    "ON template_promotion_candidates(status);"
)


def ensure_schema(db_path: Path | str) -> None:
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute(_DDL_CANDIDATES)
        conn.execute(_DDL_INDEX_TEMPLATE)
        conn.execute(_DDL_INDEX_STATUS)
        conn.commit()
        conn.close()
    except Exception:
        logger.warning(
            "template_promotion_candidates schema bootstrap failed", exc_info=True,
        )


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class PromotionCandidate:
    cluster_key: str
    target_template: str
    target_field: str | None
    suggested_regex: str
    suggested_normalization: str | None
    cluster_size: int
    member_rule_ids: list[int] = field(default_factory=list)
    sample_source_pdfs: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class PromotionDetector:
    """Scan accepted per-doc rules for promotion candidates.

    Phase 4C is detection-only — applying a promotion is out of scope.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        ensure_schema(self._db_path)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def detect_all(self) -> list[PromotionCandidate]:
        """Run the detection pass; upsert candidates; return what was found."""
        rules = self._fetch_accepted_rules()
        if not rules:
            return []
        clusters = self._cluster_rules(rules)
        out: list[PromotionCandidate] = []
        for cluster in clusters:
            if len(cluster) < MIN_CLUSTER_SIZE:
                continue
            cand = self._build_candidate(cluster)
            self._upsert(cand)
            out.append(cand)
            # Bump promotion_eligible_count on each member rule for observability.
            self._bump_member_rule_counts(cand)
        return out

    # ------------------------------------------------------------------
    # Internal — data fetch
    # ------------------------------------------------------------------

    def _fetch_accepted_rules(self) -> list[dict[str, Any]]:
        """Pull accepted rules joined with their parent document_identity row.

        Skips rules attached to documents with no consensus_top — a
        rule without a target template can't be promoted.
        """
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT dsr.id AS rule_id,
                       dsr.candidate_regex,
                       dsr.candidate_normalization,
                       dsr.target_field,
                       dsr.document_identity_id,
                       di.source_pdf,
                       di.profile_consensus_top
                FROM document_specific_rules dsr
                JOIN document_identity di
                    ON di.id = dsr.document_identity_id
                WHERE dsr.status = 'accepted'
                  AND di.profile_consensus_top IS NOT NULL
                  AND di.profile_consensus_top != ''
                """
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Internal — clustering
    # ------------------------------------------------------------------

    def _cluster_rules(
        self, rules: list[dict[str, Any]]
    ) -> list[list[dict[str, Any]]]:
        """Group rules by (consensus_top, target_field), then cluster within.

        Two rules in the same group go in the same cluster when their
        token-set Jaccard >= SIMILARITY_THRESHOLD. Cheap union-find via
        a sequential merge — order doesn't matter for finding clusters
        of >= MIN_CLUSTER_SIZE.
        """
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for r in rules:
            key = (
                r.get("profile_consensus_top") or "",
                r.get("target_field") or "",
            )
            groups[key].append(r)

        clusters: list[list[dict[str, Any]]] = []
        for group in groups.values():
            for cluster in self._cluster_within_group(group):
                clusters.append(cluster)
        return clusters

    def _cluster_within_group(
        self, group: list[dict[str, Any]]
    ) -> list[list[dict[str, Any]]]:
        if len(group) < MIN_CLUSTER_SIZE:
            return []
        token_sets = [
            (r, _tokenize_regex(r.get("candidate_regex") or ""))
            for r in group
        ]
        # Greedy clustering — for each rule, attach to the first cluster
        # whose centroid passes the threshold; otherwise start a new cluster.
        clusters: list[list[tuple[dict[str, Any], set[str]]]] = []
        for r, toks in token_sets:
            placed = False
            for cluster in clusters:
                centroid = cluster[0][1]
                if _jaccard(toks, centroid) >= SIMILARITY_THRESHOLD:
                    cluster.append((r, toks))
                    placed = True
                    break
            if not placed:
                clusters.append([(r, toks)])
        return [
            [r for r, _ in cluster]
            for cluster in clusters
            if len(cluster) >= MIN_CLUSTER_SIZE
        ]

    # ------------------------------------------------------------------
    # Internal — candidate construction
    # ------------------------------------------------------------------

    def _build_candidate(
        self, cluster: list[dict[str, Any]]
    ) -> PromotionCandidate:
        """Pick the cluster's representative regex (the most common variant).

        Ties break on shortest regex — a more specific pattern is usually
        the one we want to promote.
        """
        # Count exact-string occurrences first; fall back to first member.
        regex_counts: dict[str, int] = defaultdict(int)
        norm_counts: dict[str, int] = defaultdict(int)
        for r in cluster:
            regex_counts[r.get("candidate_regex") or ""] += 1
            norm_counts[(r.get("candidate_normalization") or "")] += 1
        best_regex = max(
            regex_counts.items(),
            key=lambda kv: (kv[1], -len(kv[0])),
        )[0]
        best_norm = max(norm_counts.items(), key=lambda kv: kv[1])[0] or None

        target_template = cluster[0].get("profile_consensus_top") or ""
        target_field = cluster[0].get("target_field") or None
        cluster_key = self._cluster_key(target_template, target_field, best_regex)

        member_ids = [int(r["rule_id"]) for r in cluster]
        sample_pdfs: list[str] = []
        for r in cluster:
            pdf = r.get("source_pdf") or ""
            if pdf and pdf not in sample_pdfs:
                sample_pdfs.append(pdf)
            if len(sample_pdfs) >= 5:
                break

        return PromotionCandidate(
            cluster_key=cluster_key,
            target_template=target_template,
            target_field=target_field,
            suggested_regex=best_regex,
            suggested_normalization=best_norm,
            cluster_size=len(cluster),
            member_rule_ids=member_ids,
            sample_source_pdfs=sample_pdfs,
        )

    @staticmethod
    def _cluster_key(target_template: str, target_field: str | None, regex: str) -> str:
        # Stable, idempotent key — regex hash keeps same regex / target / field
        # from creating duplicate candidate rows on rerun.
        import hashlib
        h = hashlib.sha1(regex.encode("utf-8")).hexdigest()[:12]
        return f"{target_template}|{target_field or ''}|{h}"

    # ------------------------------------------------------------------
    # Internal — persistence
    # ------------------------------------------------------------------

    def _upsert(self, cand: PromotionCandidate) -> None:
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.execute(
                """
                INSERT INTO template_promotion_candidates
                    (cluster_key, target_template, target_field,
                     suggested_regex, suggested_normalization,
                     cluster_size, member_rule_ids_json,
                     sample_source_pdfs_json, status, notes,
                     last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, datetime('now'))
                ON CONFLICT(cluster_key) DO UPDATE SET
                    cluster_size = excluded.cluster_size,
                    member_rule_ids_json = excluded.member_rule_ids_json,
                    sample_source_pdfs_json = excluded.sample_source_pdfs_json,
                    suggested_normalization = excluded.suggested_normalization,
                    last_updated = excluded.last_updated
                """,
                (
                    cand.cluster_key,
                    cand.target_template,
                    cand.target_field,
                    cand.suggested_regex,
                    cand.suggested_normalization,
                    cand.cluster_size,
                    json.dumps(cand.member_rule_ids),
                    json.dumps(cand.sample_source_pdfs),
                    f"detected by Phase 4C; {cand.cluster_size} per-doc rules in cluster",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _bump_member_rule_counts(self, cand: PromotionCandidate) -> None:
        """Bump `promotion_eligible_count` on each member so the per-doc
        rules dashboard surfaces the cluster signal."""
        if not cand.member_rule_ids:
            return
        placeholders = ", ".join(["?"] * len(cand.member_rule_ids))
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.execute(
                f"""
                UPDATE document_specific_rules
                SET promotion_eligible_count = ?,
                    last_updated = datetime('now')
                WHERE id IN ({placeholders})
                """,
                (cand.cluster_size, *cand.member_rule_ids),
            )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Helpers — token-set Jaccard
# ---------------------------------------------------------------------------

# Split a regex into its meaningful tokens for similarity scoring. We strip
# regex metachars (so two regexes that differ only in escaping or grouping
# still register as similar) but preserve identifier-like substrings
# (which tend to be the schedule codes / keywords that anchor a regex).
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]+")


def _tokenize_regex(regex: str) -> set[str]:
    if not regex:
        return set()
    return {t.lower() for t in _TOKEN_RE.findall(regex)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------


def fetch_promotion_summary(db_path: Path | str) -> dict[str, Any]:
    ensure_schema(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM template_promotion_candidates"
        ).fetchone()[0]
        status_rows = conn.execute(
            "SELECT status, COUNT(*) FROM template_promotion_candidates GROUP BY status"
        ).fetchall()
        by_template = conn.execute(
            """
            SELECT target_template, COUNT(*) FROM template_promotion_candidates
            WHERE status IN ('pending','approved') GROUP BY target_template
            ORDER BY 2 DESC LIMIT 20
            """
        ).fetchall()
        top_clusters = conn.execute(
            """
            SELECT cluster_key, target_template, target_field,
                   substr(suggested_regex, 1, 80) AS regex_preview,
                   cluster_size, status
            FROM template_promotion_candidates
            ORDER BY cluster_size DESC, id DESC LIMIT 10
            """
        ).fetchall()
    finally:
        conn.close()
    return {
        "total": int(total),
        "by_status": {r["status"]: int(r[1]) for r in status_rows},
        "by_template": {r[0]: int(r[1]) for r in by_template},
        "top_clusters": [dict(r) for r in top_clusters],
    }
