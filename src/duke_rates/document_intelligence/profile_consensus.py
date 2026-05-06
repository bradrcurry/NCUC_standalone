"""
Profile-consensus engine for ``wrong_profile`` parse failures.

Combines three independent, deterministic signals to recommend a parser profile
for documents that the existing classifier+parser pipeline routed incorrectly:

1. **Schedule-code lookup**  — high-specificity codes (e.g. ``RES-28``,
   ``MGS-32``) extracted by the fingerprinter, scored against the historical
   distribution of which profiles successfully parsed each code.
2. **Title-candidate match** — distinctive page titles
   (e.g. ``"RIDER PS"``, ``"NC GREENPOWER PROGRAM"``) scored against the
   distribution of which profiles produced charges for similar titles.
3. **Filename-pattern router** — keyword tokens in the source path
   (e.g. ``schedule-mgs``, ``rider-pim``, ``fcar``).

For each ``wrong_profile`` diagnosis the engine produces a ranked list of
recommended profiles with confidence scores. When the top recommendation is
both significantly above the runner-up AND distinct from the failing profile,
that recommendation is persisted to ``parser_profile_recommendations`` for
review or automated reassignment.

This is a deterministic pass — no LLM calls. Run it via the
``run-overnight-parse-improvement-nc --task-kind profile_consensus`` task
or the standalone CLI ``recommend-profile-reassignments-nc``.
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

# Only schedule codes matching this pattern are trusted as a signal — the
# fingerprinter's regex picks up many English words ("IS", "MAY", "OR")
# that are not really schedule codes. We keep tokens like RES-28, MGS-32,
# SGS76, LGS-3, etc.
HIGH_SPECIFICITY_CODE = re.compile(r"^[A-Z]{2,5}-?\d{1,3}[A-Z]?$")

# Title is distinctive enough to use as a signal when it's between these
# lengths and not a generic header. We exclude common boilerplate.
TITLE_MIN_LEN = 5
TITLE_MAX_LEN = 100
TITLE_BLOCKLIST = {
    "AVAILABILITY", "CERTIFICATE OF SERVICE", "(NORTH CAROLINA ONLY)",
    "ASSOCIATE GENERAL COUNSEL", "DEPUTY GENERAL COUNSEL", "F I L ED",
    "FILED", "DEFINITIONS", "MAILING ADDRESS:", "APPLICABILITY",
    "ENERGY.", "DUKE ENERGY", "DUKE ENERGY CAROLINAS",
}

# Filename keyword router — ordered by specificity (more specific first).
# Each entry: (regex, recommended_profile, weight).
FILENAME_RULES: list[tuple[re.Pattern[str], str, float]] = [
    (re.compile(r"\bgreenpower\b", re.IGNORECASE), "progress_greenpower_program", 0.9),
    (re.compile(r"\bnc-greenpower\b", re.IGNORECASE), "progress_greenpower_program", 0.95),
    (re.compile(r"\bsolar-choice\b|\brider[-_]ns?c\b", re.IGNORECASE), "carolinas_solar_choice_rider", 0.9),
    (re.compile(r"\bnet-metering\b|\brider[-_]nmb\b", re.IGNORECASE), "carolinas_solar_choice_rider", 0.85),
    (re.compile(r"\bpowerpair\b|\brider[-_]pp\b", re.IGNORECASE), "progress_powerpair_pilot", 0.9),
    (re.compile(r"\bpower[-_]?manager\b|\brider[-_]pm\b", re.IGNORECASE), "carolinas_residential_load_control", 0.8),
    (re.compile(r"\bsunsense\b", re.IGNORECASE), "progress_sunsense_solar_rebate", 0.9),
    (re.compile(r"\bfcar\b|\bfuel[-_]cost\b", re.IGNORECASE), "carolinas_fuel_cost_adj_rider", 0.85),
    (re.compile(r"\benergywise\b", re.IGNORECASE), "progress_energywise_business", 0.85),
    (re.compile(r"\bschedule[-_]res\b|\bschedule[-_]r[-_]?\d", re.IGNORECASE), "progress_residential_flat", 0.7),
    (re.compile(r"\bschedule[-_]sgs\b", re.IGNORECASE), "carolinas_general_service_schedule", 0.7),
    (re.compile(r"\bschedule[-_]mgs\b|\bmedium[-_]general\b", re.IGNORECASE), "progress_mgs", 0.7),
    (re.compile(r"\bschedule[-_]lgs\b|\blarge[-_]general\b", re.IGNORECASE), "carolinas_general_service_schedule", 0.7),
    (re.compile(r"\b(?:ee|energy[-_]efficiency)[-_]rider\b", re.IGNORECASE), "carolinas_energy_efficiency_rider", 0.8),
    (re.compile(r"\bdsm\b", re.IGNORECASE), "carolinas_single_value_rider", 0.6),
    (re.compile(r"\bload[-_]control\b", re.IGNORECASE), "progress_residential_load_control", 0.6),
    (re.compile(r"\btou[-_]cpp\b|\btou[-_]ev\b", re.IGNORECASE), "progress_residential_tou", 0.7),
    (re.compile(r"\brider\b.*\bri[-_]?\d", re.IGNORECASE), "progress_single_value_rider", 0.6),
    (re.compile(r"\bstorm[-_]securitization\b", re.IGNORECASE), "progress_storm_securitization", 0.9),
    (re.compile(r"\bnet[-_]metering\b", re.IGNORECASE), "carolinas_net_metering_rider", 0.7),
    (re.compile(r"\beconomic[-_]development\b", re.IGNORECASE), "carolinas_economic_development_rider", 0.85),
    (re.compile(r"\blighting\b", re.IGNORECASE), "carolinas_lighting_schedule", 0.7),
]

# Confidence threshold to actually emit a recommendation — below this we
# leave the case for human review rather than auto-suggest.
RECOMMEND_MIN_CONFIDENCE = 0.55
# Margin the top recommendation must beat the runner-up by.
RECOMMEND_MIN_MARGIN = 0.15


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS parser_profile_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parse_attempt_id INTEGER NOT NULL,
    diagnosis_id INTEGER,
    source_pdf TEXT NOT NULL,
    failing_profile TEXT NOT NULL,
    recommended_profile TEXT NOT NULL,
    confidence REAL NOT NULL,
    margin REAL NOT NULL,
    votes_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_review',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_DDL_INDEX = """
CREATE INDEX IF NOT EXISTS idx_recommendations_parse_attempt
    ON parser_profile_recommendations(parse_attempt_id);
"""


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@dataclass
class ProfileVote:
    profile: str
    weight: float
    reason: str


@dataclass
class ProfileRecommendation:
    parse_attempt_id: int
    source_pdf: str
    failing_profile: str
    top_profile: str = ""
    confidence: float = 0.0
    margin: float = 0.0
    votes: list[ProfileVote] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    status: str = "no_recommendation"  # 'recommended' | 'no_recommendation' | 'failing_already_best'


class ProfileConsensusEngine:
    """Score wrong_profile diagnoses against the historical successful-parse distribution."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._code_to_profiles: dict[str, dict[str, int]] | None = None
        self._title_to_profiles: dict[str, dict[str, int]] | None = None
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def select_pending_diagnoses(self, *, limit: int = 25) -> list[dict[str, Any]]:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT ld.id AS diagnosis_id,
                       pal.id AS parse_attempt_id,
                       pal.source_pdf,
                       pal.parser_profile AS failing_profile
                FROM llm_parse_diagnostics ld
                JOIN parse_attempt_logs pal ON pal.id = ld.parse_attempt_id
                WHERE ld.failure_type = 'wrong_profile'
                  AND pal.id NOT IN (
                      SELECT parse_attempt_id FROM parser_profile_recommendations
                  )
                ORDER BY ld.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def recommend_all_pending(self, *, limit: int = 25) -> list[ProfileRecommendation]:
        # Load profile-distribution lookups once per batch.
        self._build_lookups()
        pending = self.select_pending_diagnoses(limit=limit)
        out: list[ProfileRecommendation] = []
        for row in pending:
            try:
                rec = self.recommend_for_diagnosis(row)
                self._persist_recommendation(row.get("diagnosis_id"), rec)
                out.append(rec)
            except Exception as exc:
                logger.warning(
                    "profile recommendation failed for parse_attempt=%s: %s",
                    row.get("parse_attempt_id"), exc, exc_info=True,
                )
        return out

    def recommend_for_diagnosis(self, row: dict[str, Any]) -> ProfileRecommendation:
        if self._code_to_profiles is None:
            self._build_lookups()
        parse_attempt_id = int(row.get("parse_attempt_id", 0))
        source_pdf = row.get("source_pdf") or ""
        failing_profile = row.get("failing_profile") or "unknown"

        rec = ProfileRecommendation(
            parse_attempt_id=parse_attempt_id,
            source_pdf=source_pdf,
            failing_profile=failing_profile,
        )

        fp = self._fetch_fingerprint(source_pdf)
        codes = self._extract_codes(fp.get("schedule_codes_json"))
        rider_codes = self._extract_codes(fp.get("rider_codes_json"))
        titles = self._extract_titles(fp.get("title_candidates_json"))

        rec.evidence = {
            "schedule_codes": codes,
            "rider_codes": rider_codes,
            "titles": titles[:5],
        }

        # Aggregate weighted votes per candidate profile.
        scores: dict[str, float] = defaultdict(float)
        for c in codes + rider_codes:
            for prof, weight, reason in self._votes_for_code(c):
                scores[prof] += weight
                rec.votes.append(ProfileVote(profile=prof, weight=weight, reason=reason))
        for t in titles:
            for prof, weight, reason in self._votes_for_title(t):
                scores[prof] += weight
                rec.votes.append(ProfileVote(profile=prof, weight=weight, reason=reason))
        for pat, prof, weight in FILENAME_RULES:
            if pat.search(source_pdf):
                scores[prof] += weight
                rec.votes.append(ProfileVote(
                    profile=prof, weight=weight,
                    reason=f"filename matches {pat.pattern}",
                ))

        if not scores:
            rec.status = "no_recommendation"
            return rec

        # Normalize so the top vote is at most 1.0
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        top_profile, top_score = ranked[0]
        runner_up_score = ranked[1][1] if len(ranked) > 1 else 0.0
        max_possible = max(top_score, 1.0)
        rec.top_profile = top_profile
        rec.confidence = round(top_score / max_possible, 3)
        rec.margin = round((top_score - runner_up_score) / max_possible, 3)

        if top_profile == failing_profile:
            rec.status = "failing_already_best"
        elif rec.confidence >= RECOMMEND_MIN_CONFIDENCE and rec.margin >= RECOMMEND_MIN_MARGIN:
            rec.status = "recommended"
        else:
            rec.status = "no_recommendation"

        return rec

    # ------------------------------------------------------------------
    # Internal — vote functions
    # ------------------------------------------------------------------

    def _votes_for_code(self, code: str) -> list[tuple[str, float, str]]:
        if not code or self._code_to_profiles is None:
            return []
        if not HIGH_SPECIFICITY_CODE.match(code):
            return []
        profiles = self._code_to_profiles.get(code)
        if not profiles:
            return []
        total = sum(profiles.values())
        if total < 5:
            # Too few historical examples to trust.
            return []
        out: list[tuple[str, float, str]] = []
        for prof, n in profiles.items():
            purity = n / total
            if purity >= 0.30:  # ignore profiles that rarely parse this code
                weight = round(purity * min(1.0, total / 50.0), 3)
                out.append((prof, weight, f"schedule_code={code} parsed by {prof} {n}/{total}"))
        return out

    def _votes_for_title(self, title: str) -> list[tuple[str, float, str]]:
        if not title or self._title_to_profiles is None:
            return []
        t = title.upper().strip()
        if t in TITLE_BLOCKLIST or len(t) < TITLE_MIN_LEN or len(t) > TITLE_MAX_LEN:
            return []
        profiles = self._title_to_profiles.get(t)
        if not profiles:
            return []
        total = sum(profiles.values())
        if total < 3:
            return []
        out: list[tuple[str, float, str]] = []
        for prof, n in profiles.items():
            purity = n / total
            if purity >= 0.40:
                weight = round(purity * min(1.0, total / 20.0) * 0.7, 3)
                out.append((prof, weight, f"title={t[:60]!r} parsed by {prof} {n}/{total}"))
        return out

    # ------------------------------------------------------------------
    # Internal — lookup builders
    # ------------------------------------------------------------------

    def _build_lookups(self) -> None:
        if self._code_to_profiles is not None:
            return
        codes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        titles: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT pal.parser_profile,
                       fp.schedule_codes_json,
                       fp.rider_codes_json,
                       fp.title_candidates_json
                FROM parse_attempt_logs pal
                JOIN document_fingerprints_v2 fp ON fp.source_pdf = pal.source_pdf
                WHERE pal.status = 'parsed'
                  AND pal.charge_count > 0
                  AND pal.parser_profile IS NOT NULL
                  AND pal.parser_profile != ''
                """
            ).fetchall()
        finally:
            conn.close()

        for r in rows:
            profile = r["parser_profile"]
            for c in self._extract_codes(r["schedule_codes_json"]):
                if HIGH_SPECIFICITY_CODE.match(c):
                    codes[c][profile] += 1
            for c in self._extract_codes(r["rider_codes_json"]):
                if HIGH_SPECIFICITY_CODE.match(c):
                    codes[c][profile] += 1
            for t in self._extract_titles(r["title_candidates_json"]):
                t_norm = t.upper().strip()
                if t_norm in TITLE_BLOCKLIST:
                    continue
                if not (TITLE_MIN_LEN <= len(t_norm) <= TITLE_MAX_LEN):
                    continue
                titles[t_norm][profile] += 1

        self._code_to_profiles = {k: dict(v) for k, v in codes.items()}
        self._title_to_profiles = {k: dict(v) for k, v in titles.items()}

    def _fetch_fingerprint(self, source_pdf: str) -> dict[str, Any]:
        if not source_pdf:
            return {}
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            r = conn.execute(
                "SELECT * FROM document_fingerprints_v2 WHERE source_pdf = ? LIMIT 1",
                (source_pdf,),
            ).fetchone()
            return dict(r) if r else {}
        finally:
            conn.close()

    @staticmethod
    def _extract_codes(json_str: str | None) -> list[str]:
        try:
            data = json.loads(json_str or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
        return [c for c in data if isinstance(c, str)]

    @staticmethod
    def _extract_titles(json_str: str | None) -> list[str]:
        try:
            data = json.loads(json_str or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
        return [t for t in data if isinstance(t, str)]

    # ------------------------------------------------------------------
    # Internal — persistence
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute(_DDL)
            conn.execute(_DDL_INDEX)
            conn.commit()
            conn.close()
        except Exception:
            logger.warning("recommendations schema bootstrap failed", exc_info=True)

    def _persist_recommendation(
        self, diagnosis_id: int | None, rec: ProfileRecommendation
    ) -> None:
        if rec.status not in ("recommended", "failing_already_best", "no_recommendation"):
            return
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute(
                """
                INSERT INTO parser_profile_recommendations
                    (parse_attempt_id, diagnosis_id, source_pdf, failing_profile,
                     recommended_profile, confidence, margin, votes_json,
                     evidence_json, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec.parse_attempt_id,
                    diagnosis_id,
                    rec.source_pdf,
                    rec.failing_profile,
                    rec.top_profile or "",
                    rec.confidence,
                    rec.margin,
                    json.dumps([
                        {"profile": v.profile, "weight": v.weight, "reason": v.reason}
                        for v in rec.votes
                    ]),
                    json.dumps(rec.evidence),
                    rec.status,
                ),
            )
            conn.commit()
            conn.close()
        except Exception:
            logger.warning(
                "failed to persist recommendation for parse_attempt=%s",
                rec.parse_attempt_id, exc_info=True,
            )

    # ------------------------------------------------------------------
    # Diagnosis reclassifier
    # ------------------------------------------------------------------

    def reclassify_failing_already_best(
        self, *, target_failure_type: str = "regex_gap"
    ) -> int:
        """Reclassify ``wrong_profile`` diagnoses where consensus said the
        failing profile IS the best fit.

        Those cases aren't actually mis-routed — the parser ran on the right
        profile and still extracted nothing. Re-labeling them as
        ``regex_gap`` makes them eligible for the suggest stage on the next
        run.

        Returns the number of diagnoses updated.
        """
        try:
            conn = sqlite3.connect(str(self._db_path))
            cur = conn.execute(
                """
                UPDATE llm_parse_diagnostics
                SET failure_type = ?,
                    notes = COALESCE(notes,'')
                            || ' [reclassified by profile_consensus: '
                            || 'failing_already_best -> ' || ? || ']'
                WHERE id IN (
                    SELECT diagnosis_id
                    FROM parser_profile_recommendations
                    WHERE status = 'failing_already_best'
                      AND diagnosis_id IS NOT NULL
                )
                AND failure_type = 'wrong_profile'
                """,
                (target_failure_type, target_failure_type),
            )
            updated = cur.rowcount
            conn.commit()
            conn.close()
            return updated
        except Exception:
            logger.warning(
                "reclassify_failing_already_best failed", exc_info=True,
            )
            return 0
