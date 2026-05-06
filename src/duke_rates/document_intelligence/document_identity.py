"""
Document identity layer (Phase 1 of parsing-architecture refactor).

Aggregates every signal we have about a document — schedule codes, rider
codes, leaf numbers, page titles, classifier output, profile consensus,
filename heuristics — into a single ``document_identity`` row per
``source_pdf`` with a confidence score and an append-only evidence log.

This layer is **read-only output** in Phase 1: it does not change extraction
behavior. Phase 2 (routing tier system) and Phase 3 (template binding)
consume identity bundles to make routing decisions.

Plan reference: ``docs/PARSING_ARCHITECTURE_REFACTOR_PLAN.md`` §4.

Usage::

    from duke_rates.document_intelligence.document_identity import (
        DocumentIdentityAggregator, ensure_schema,
    )
    ensure_schema(db_path)
    agg = DocumentIdentityAggregator(db_path)
    n = agg.populate_all()
    print(f"populated {n} identity rows")
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL_DOCUMENT_IDENTITY = """
CREATE TABLE IF NOT EXISTS document_identity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_pdf TEXT NOT NULL UNIQUE,

    -- Strong (high-specificity) signals from the fingerprinter
    schedule_codes_strong_json TEXT NOT NULL DEFAULT '[]',
    rider_codes_strong_json TEXT NOT NULL DEFAULT '[]',
    leaf_numbers_json TEXT NOT NULL DEFAULT '[]',
    detected_titles_json TEXT NOT NULL DEFAULT '[]',
    filename_signals_json TEXT NOT NULL DEFAULT '[]',

    -- Classifier consensus (from document_classifications)
    classifier_label TEXT,
    classifier_confidence REAL,

    -- Profile consensus (from parser_profile_recommendations)
    profile_consensus_top TEXT,
    profile_consensus_confidence REAL,
    profile_consensus_margin REAL,

    -- Inferences derived from above
    inferred_family TEXT,
    inferred_doc_type TEXT,
    inferred_effective_date TEXT,

    -- Overall identity confidence (0.0 - 1.0)
    overall_confidence REAL NOT NULL DEFAULT 0.0,

    -- Append-only log of evidence inputs
    evidence_log_json TEXT NOT NULL DEFAULT '[]',

    last_updated TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_DDL_INDEX_PDF = (
    "CREATE INDEX IF NOT EXISTS idx_document_identity_pdf "
    "ON document_identity(source_pdf);"
)
_DDL_INDEX_CONFIDENCE = (
    "CREATE INDEX IF NOT EXISTS idx_document_identity_confidence "
    "ON document_identity(overall_confidence DESC);"
)


def ensure_schema(db_path: Path | str) -> None:
    """Create the ``document_identity`` table and its indexes if missing.

    Idempotent — safe to call from any module. Phase 1B's aggregator and
    Phase 1C's CLI both call this on init.
    """
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute(_DDL_DOCUMENT_IDENTITY)
        conn.execute(_DDL_INDEX_PDF)
        conn.execute(_DDL_INDEX_CONFIDENCE)
        conn.commit()
        conn.close()
    except Exception:
        logger.warning("document_identity schema bootstrap failed", exc_info=True)


# ---------------------------------------------------------------------------
# Constants — match Phase 0B helpers + profile_consensus
# ---------------------------------------------------------------------------

# High-specificity schedule/rider code pattern (matches RES-28, MGS-32, etc.)
HIGH_SPECIFICITY_CODE_RE = re.compile(r"^[A-Z]{2,5}-?\d{1,3}[A-Z]?$")

# Title fragments that show up across many profiles and shouldn't count as
# distinctive evidence. Mirrors ANCHOR_TITLE_BLOCKLIST in regex_suggestions
# and TITLE_BLOCKLIST in profile_consensus.
TITLE_BLOCKLIST = {
    "AVAILABILITY", "CERTIFICATE OF SERVICE", "(NORTH CAROLINA ONLY)",
    "ASSOCIATE GENERAL COUNSEL", "DEPUTY GENERAL COUNSEL", "F I L ED",
    "FILED", "DEFINITIONS", "MAILING ADDRESS:", "APPLICABILITY",
    "ENERGY.", "DUKE ENERGY", "DUKE ENERGY CAROLINAS",
}

TITLE_MIN_LEN = 5
TITLE_MAX_LEN = 100

# Filename heuristic rules — cheap pattern match against the source path.
# Each entry is (regex, signal_label). Multiple may match per filename.
FILENAME_SIGNAL_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bgreenpower\b", re.IGNORECASE),                 "greenpower"),
    (re.compile(r"\bsolar[-_]choice\b", re.IGNORECASE),            "solar_choice"),
    (re.compile(r"\brider[-_]ns?c\b", re.IGNORECASE),              "rider_nsc"),
    (re.compile(r"\bnet[-_]metering\b|\brider[-_]nmb\b", re.IGNORECASE), "net_metering"),
    (re.compile(r"\bpowerpair\b|\brider[-_]pp\b", re.IGNORECASE),  "powerpair"),
    (re.compile(r"\bpower[-_]?manager\b|\brider[-_]pm\b", re.IGNORECASE), "power_manager"),
    (re.compile(r"\bsunsense\b", re.IGNORECASE),                   "sunsense"),
    (re.compile(r"\bfcar\b|\bfuel[-_]cost\b", re.IGNORECASE),      "fcar"),
    (re.compile(r"\benergywise\b", re.IGNORECASE),                 "energywise"),
    (re.compile(r"\bschedule[-_]res\b|\bschedule[-_]r[-_]?\d", re.IGNORECASE), "schedule_res"),
    (re.compile(r"\bschedule[-_]sgs\b", re.IGNORECASE),            "schedule_sgs"),
    (re.compile(r"\bschedule[-_]mgs\b|\bmedium[-_]general\b", re.IGNORECASE), "schedule_mgs"),
    (re.compile(r"\bschedule[-_]lgs\b|\blarge[-_]general\b", re.IGNORECASE), "schedule_lgs"),
    (re.compile(r"\b(?:ee|energy[-_]efficiency)[-_]rider\b", re.IGNORECASE), "ee_rider"),
    (re.compile(r"\bdsm\b", re.IGNORECASE),                        "dsm"),
    (re.compile(r"\bload[-_]control\b", re.IGNORECASE),            "load_control"),
    (re.compile(r"\btou[-_]cpp\b|\btou[-_]ev\b", re.IGNORECASE),   "residential_tou"),
    (re.compile(r"\bstorm[-_]securitization\b", re.IGNORECASE),    "storm_securitization"),
    (re.compile(r"\beconomic[-_]development\b", re.IGNORECASE),    "economic_development"),
    (re.compile(r"\blighting\b", re.IGNORECASE),                   "lighting"),
    (re.compile(r"\brider\b", re.IGNORECASE),                      "rider"),
    (re.compile(r"\bcompliance\b", re.IGNORECASE),                 "compliance"),
    (re.compile(r"\bredline\b|\bproposed\b", re.IGNORECASE),       "redline_or_proposed"),
]


# Confidence weights — initial values per plan §4.1B. Tunable from the
# identity-quality report (1D).
WEIGHT_SCHEDULE_CODE = 0.30      # at least one strong schedule code
WEIGHT_DISTINCTIVE_TITLE = 0.20  # at least one distinctive title
WEIGHT_FILENAME_SIGNAL = 0.15    # at least one filename rule hit
WEIGHT_CLASSIFIER = 0.15         # classifier_confidence >= 0.7
WEIGHT_PROFILE_CONSENSUS = 0.20  # consensus_confidence >= 0.7 AND margin >= 0.15


# ---------------------------------------------------------------------------
# Data class for the bundle
# ---------------------------------------------------------------------------


@dataclass
class IdentityBundle:
    source_pdf: str
    schedule_codes_strong: list[str] = field(default_factory=list)
    rider_codes_strong: list[str] = field(default_factory=list)
    leaf_numbers: list[str] = field(default_factory=list)
    detected_titles: list[str] = field(default_factory=list)
    filename_signals: list[str] = field(default_factory=list)
    classifier_label: str | None = None
    classifier_confidence: float | None = None
    profile_consensus_top: str | None = None
    profile_consensus_confidence: float | None = None
    profile_consensus_margin: float | None = None
    inferred_family: str | None = None
    inferred_doc_type: str | None = None
    inferred_effective_date: str | None = None
    overall_confidence: float = 0.0
    evidence_log: list[dict[str, Any]] = field(default_factory=list)

    def to_persistence_tuple(self) -> tuple[Any, ...]:
        return (
            self.source_pdf,
            json.dumps(self.schedule_codes_strong),
            json.dumps(self.rider_codes_strong),
            json.dumps(self.leaf_numbers),
            json.dumps(self.detected_titles),
            json.dumps(self.filename_signals),
            self.classifier_label,
            self.classifier_confidence,
            self.profile_consensus_top,
            self.profile_consensus_confidence,
            self.profile_consensus_margin,
            self.inferred_family,
            self.inferred_doc_type,
            self.inferred_effective_date,
            self.overall_confidence,
            json.dumps(self.evidence_log, default=str),
            datetime.now(timezone.utc).isoformat(),
        )


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


class DocumentIdentityAggregator:
    """Build identity bundles by aggregating existing tables.

    Sources:
      - ``document_fingerprints_v2`` (schedule codes, rider codes, leaf
        numbers, title candidates).
      - ``document_classifications`` (classifier label + confidence).
      - ``parser_profile_recommendations`` (profile consensus).
      - filename heuristics (pure-Python regex against ``source_pdf``).

    The aggregator is deterministic and idempotent — populate_all() can be
    called repeatedly. Existing rows are upserted by ``source_pdf``.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        ensure_schema(self._db_path)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def select_source_pdfs(self, *, limit: int | None = None) -> list[str]:
        """Pick all source_pdfs that have at least one evidence source.

        Currently this is the union of source_pdf values across
        document_fingerprints_v2, document_classifications, and
        parser_profile_recommendations.
        """
        conn = sqlite3.connect(str(self._db_path))
        try:
            sql = """
            SELECT DISTINCT source_pdf FROM (
                SELECT source_pdf FROM document_fingerprints_v2
                UNION
                SELECT subject_id AS source_pdf FROM document_classifications
                    WHERE subject_kind IN ('source_pdf','document','pdf')
                UNION
                SELECT source_pdf FROM parser_profile_recommendations
            )
            WHERE source_pdf IS NOT NULL AND source_pdf != ''
            """
            if limit:
                sql += f" LIMIT {int(limit)}"
            rows = conn.execute(sql).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    def build_bundle(self, source_pdf: str) -> IdentityBundle:
        """Build a single identity bundle without persisting it.

        Useful for callers that want to inspect the bundle before deciding
        whether to write it (e.g. dry-run reports).
        """
        bundle = IdentityBundle(source_pdf=source_pdf)
        self._add_fingerprint_evidence(bundle)
        self._add_classifier_evidence(bundle)
        self._add_consensus_evidence(bundle)
        self._add_filename_evidence(bundle)
        bundle.overall_confidence = self._score(bundle)
        return bundle

    def populate_all(self, *, limit: int | None = None) -> int:
        """Build and upsert identity rows for every source_pdf with evidence.

        Returns the number of rows written.
        """
        pdfs = self.select_source_pdfs(limit=limit)
        n = 0
        for pdf in pdfs:
            try:
                bundle = self.build_bundle(pdf)
                self._upsert(bundle)
                n += 1
            except Exception:
                logger.warning(
                    "identity bundle failed for %s", pdf, exc_info=True,
                )
        return n

    # ------------------------------------------------------------------
    # Internal — evidence collectors
    # ------------------------------------------------------------------

    def _add_fingerprint_evidence(self, bundle: IdentityBundle) -> None:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT schedule_codes_json, rider_codes_json,
                       leaf_numbers_json, title_candidates_json
                FROM document_fingerprints_v2
                WHERE source_pdf = ? LIMIT 1
                """,
                (bundle.source_pdf,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return

        def _load(j: Any) -> list[str]:
            try:
                data = json.loads(j or "[]")
            except (json.JSONDecodeError, TypeError):
                return []
            return [s for s in data if isinstance(s, str)]

        for c in _load(row["schedule_codes_json"]):
            if HIGH_SPECIFICITY_CODE_RE.match(c) and c not in bundle.schedule_codes_strong:
                bundle.schedule_codes_strong.append(c)
        for c in _load(row["rider_codes_json"]):
            if HIGH_SPECIFICITY_CODE_RE.match(c) and c not in bundle.rider_codes_strong:
                bundle.rider_codes_strong.append(c)
        for c in _load(row["leaf_numbers_json"]):
            if c.strip() and c.strip() not in bundle.leaf_numbers:
                bundle.leaf_numbers.append(c.strip())
        for t in _load(row["title_candidates_json"]):
            t_norm = t.upper().strip()
            if (
                TITLE_MIN_LEN <= len(t_norm) <= TITLE_MAX_LEN
                and t_norm not in TITLE_BLOCKLIST
                and t_norm not in bundle.detected_titles
            ):
                bundle.detected_titles.append(t_norm)

        if (
            bundle.schedule_codes_strong
            or bundle.rider_codes_strong
            or bundle.detected_titles
            or bundle.leaf_numbers
        ):
            bundle.evidence_log.append({
                "source": "document_fingerprints_v2",
                "schedule_codes": bundle.schedule_codes_strong,
                "rider_codes": bundle.rider_codes_strong,
                "leaf_numbers": bundle.leaf_numbers,
                "title_count": len(bundle.detected_titles),
            })

    def _add_classifier_evidence(self, bundle: IdentityBundle) -> None:
        """Look up classifier output via the historical_documents join.

        The ``document_classifications`` table stores rows with
        ``subject_kind = 'historical_document'`` and ``subject_id`` set to
        ``historical_documents.id``. Bridge through ``local_path`` (= our
        ``source_pdf``) to find the right classification.
        """
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT dc.label, dc.confidence
                FROM document_classifications dc
                JOIN historical_documents hd
                    ON CAST(dc.subject_id AS INTEGER) = hd.id
                WHERE dc.subject_kind = 'historical_document'
                  AND dc.superseded_by IS NULL
                  AND hd.local_path = ?
                ORDER BY dc.confidence DESC, dc.id DESC
                LIMIT 1
                """,
                (bundle.source_pdf,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return
        bundle.classifier_label = row["label"]
        bundle.classifier_confidence = row["confidence"]
        bundle.evidence_log.append({
            "source": "document_classifications",
            "label": row["label"],
            "confidence": row["confidence"],
        })

    def _add_consensus_evidence(self, bundle: IdentityBundle) -> None:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT recommended_profile, confidence, margin, status
                FROM parser_profile_recommendations
                WHERE source_pdf = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (bundle.source_pdf,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return
        bundle.profile_consensus_top = row["recommended_profile"] or None
        bundle.profile_consensus_confidence = row["confidence"]
        bundle.profile_consensus_margin = row["margin"]
        bundle.evidence_log.append({
            "source": "parser_profile_recommendations",
            "recommended_profile": row["recommended_profile"],
            "confidence": row["confidence"],
            "margin": row["margin"],
            "status": row["status"],
        })

    def _add_filename_evidence(self, bundle: IdentityBundle) -> None:
        for pattern, label in FILENAME_SIGNAL_RULES:
            if pattern.search(bundle.source_pdf):
                if label not in bundle.filename_signals:
                    bundle.filename_signals.append(label)
        if bundle.filename_signals:
            bundle.evidence_log.append({
                "source": "filename_heuristics",
                "signals": bundle.filename_signals,
            })

    # ------------------------------------------------------------------
    # Internal — scoring
    # ------------------------------------------------------------------

    def _score(self, bundle: IdentityBundle) -> float:
        score = 0.0
        if bundle.schedule_codes_strong:
            score += WEIGHT_SCHEDULE_CODE
        # Distinctive title — count any that survived the blocklist
        if bundle.detected_titles:
            score += WEIGHT_DISTINCTIVE_TITLE
        if bundle.filename_signals:
            score += WEIGHT_FILENAME_SIGNAL
        if (bundle.classifier_confidence or 0.0) >= 0.7:
            score += WEIGHT_CLASSIFIER
        if (
            (bundle.profile_consensus_confidence or 0.0) >= 0.7
            and (bundle.profile_consensus_margin or 0.0) >= 0.15
        ):
            score += WEIGHT_PROFILE_CONSENSUS
        return min(1.0, round(score, 3))

    # ------------------------------------------------------------------
    # Internal — persistence
    # ------------------------------------------------------------------

    def _upsert(self, bundle: IdentityBundle) -> None:
        cols = (
            "source_pdf, schedule_codes_strong_json, rider_codes_strong_json, "
            "leaf_numbers_json, detected_titles_json, filename_signals_json, "
            "classifier_label, classifier_confidence, "
            "profile_consensus_top, profile_consensus_confidence, profile_consensus_margin, "
            "inferred_family, inferred_doc_type, inferred_effective_date, "
            "overall_confidence, evidence_log_json, last_updated"
        )
        placeholders = ", ".join(["?"] * 17)
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.execute(
                f"""
                INSERT INTO document_identity ({cols})
                VALUES ({placeholders})
                ON CONFLICT(source_pdf) DO UPDATE SET
                    schedule_codes_strong_json = excluded.schedule_codes_strong_json,
                    rider_codes_strong_json = excluded.rider_codes_strong_json,
                    leaf_numbers_json = excluded.leaf_numbers_json,
                    detected_titles_json = excluded.detected_titles_json,
                    filename_signals_json = excluded.filename_signals_json,
                    classifier_label = excluded.classifier_label,
                    classifier_confidence = excluded.classifier_confidence,
                    profile_consensus_top = excluded.profile_consensus_top,
                    profile_consensus_confidence = excluded.profile_consensus_confidence,
                    profile_consensus_margin = excluded.profile_consensus_margin,
                    inferred_family = excluded.inferred_family,
                    inferred_doc_type = excluded.inferred_doc_type,
                    inferred_effective_date = excluded.inferred_effective_date,
                    overall_confidence = excluded.overall_confidence,
                    evidence_log_json = excluded.evidence_log_json,
                    last_updated = excluded.last_updated
                """,
                bundle.to_persistence_tuple(),
            )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Read API for downstream consumers
# ---------------------------------------------------------------------------


def fetch_identity(db_path: Path | str, source_pdf: str) -> dict[str, Any] | None:
    """Return the persisted identity bundle for one source_pdf, or None."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM document_identity WHERE source_pdf = ? LIMIT 1",
            (source_pdf,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def fetch_identity_summary(db_path: Path | str) -> dict[str, Any]:
    """High-level distribution of identity confidence and signal coverage.

    Used by the Phase 1D quality report.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        total = conn.execute("SELECT COUNT(*) FROM document_identity").fetchone()[0]
        buckets = conn.execute(
            """
            SELECT
                CASE
                    WHEN overall_confidence >= 0.85 THEN 'high (>=0.85)'
                    WHEN overall_confidence >= 0.5  THEN 'mid (0.5-0.85)'
                    ELSE 'low (<0.5)'
                END AS bucket,
                COUNT(*) AS cnt
            FROM document_identity
            GROUP BY 1
            """
        ).fetchall()
        with_codes = conn.execute(
            "SELECT COUNT(*) FROM document_identity WHERE schedule_codes_strong_json != '[]'"
        ).fetchone()[0]
        with_titles = conn.execute(
            "SELECT COUNT(*) FROM document_identity WHERE detected_titles_json != '[]'"
        ).fetchone()[0]
        with_consensus = conn.execute(
            "SELECT COUNT(*) FROM document_identity WHERE profile_consensus_top IS NOT NULL"
        ).fetchone()[0]
        with_classifier = conn.execute(
            "SELECT COUNT(*) FROM document_identity WHERE classifier_label IS NOT NULL"
        ).fetchone()[0]
        return {
            "total": total,
            "confidence_buckets": {b[0]: b[1] for b in buckets},
            "coverage": {
                "with_schedule_codes": with_codes,
                "with_distinctive_titles": with_titles,
                "with_profile_consensus": with_consensus,
                "with_classifier_label": with_classifier,
            },
        }
    finally:
        conn.close()
