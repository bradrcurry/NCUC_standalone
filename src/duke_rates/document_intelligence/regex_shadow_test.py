"""
Corpus-wide shadow testing of regex suggestions.

Where ``regex_validation.py`` does a small spot-check (5 success + 3 failed +
20 unrelated docs), the shadow harness runs a candidate regex against the
**entire** target-profile corpus plus a configurable sample of unrelated
profiles. Per-doc deltas are persisted to ``llm_regex_shadow_results`` so the
behavior is auditable, and aggregate signals decide whether to promote a
suggestion from ``accepted_synthetic`` to ``accepted_strong``.

Inputs:
    suggestions in status ``accepted_synthetic`` (the new tier from the
    revised ``regex_validation.py``).

Outputs:
    - per-doc rows in ``llm_regex_shadow_results``
    - updated ``llm_regex_suggestions.status``:
        * ``accepted_strong``   — improvement on real failed docs, no regression,
          unit/value sanity checks pass
        * ``rejected_shadow``   — caused regression on real parsed docs,
          unit-mismatch on extracted values, or matched too broadly across
          unrelated profiles
        * unchanged otherwise   — leaves the suggestion at ``accepted_synthetic``
          for future re-runs (e.g. when more failed-doc text becomes available)

This is a deterministic batch — no LLM calls.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Acceptance thresholds — tunable
# ---------------------------------------------------------------------------

# Minimum number of failed-status docs the regex must add charges to.
SHADOW_MIN_IMPROVED_DOCS = 3
# Maximum number of parsed-status docs we tolerate losing charges on.
SHADOW_MAX_REGRESSED_DOCS = 0
# Maximum fraction of unrelated-profile docs the regex may match before we call
# it too broad.
SHADOW_MAX_FP_RATE = 0.05
# Minimum unrelated-profile sample size required to trust the FP rate.
SHADOW_MIN_FP_SAMPLE = 30
# Plausible range for a per-kWh charge value, in dollars. Anything outside this
# band is treated as a unit-mismatch signal (e.g. matched a paragraph number).
SHADOW_VALUE_LOW = 0.0001
SHADOW_VALUE_HIGH = 1.0
# Maximum corpus size to sweep per profile. Picks the most-recent rows when
# capped — avoids pathological runs against the largest profiles.
SHADOW_PROFILE_CORPUS_CAP = 800


@dataclass
class ShadowDocResult:
    source_pdf: str
    profile: str
    doc_status: str  # parse_attempt_logs.status
    before_count: int
    after_count: int
    extracted_values: list[float] = field(default_factory=list)
    out_of_range_values: int = 0


@dataclass
class ShadowAggregate:
    suggestion_id: int
    target_profile: str
    docs_tested: int = 0
    improved_docs: int = 0           # failed-status docs where after > before
    regressed_docs: int = 0          # parsed-status docs where after < before
    unrelated_match_docs: int = 0
    unrelated_total: int = 0
    out_of_range_total: int = 0
    final_status: str = ""
    notes: str = ""


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

_DDL_SHADOW_RESULTS = """
CREATE TABLE IF NOT EXISTS llm_regex_shadow_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    suggestion_id INTEGER NOT NULL,
    source_pdf TEXT NOT NULL,
    parser_profile TEXT NOT NULL,
    doc_status TEXT NOT NULL,
    before_count INTEGER NOT NULL DEFAULT 0,
    after_count INTEGER NOT NULL DEFAULT 0,
    extracted_values_json TEXT NOT NULL DEFAULT '[]',
    out_of_range_count INTEGER NOT NULL DEFAULT 0,
    delta_kind TEXT NOT NULL,  -- 'improved' | 'regressed' | 'unchanged' | 'unrelated_match' | 'unrelated_clean'
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_DDL_SHADOW_INDEX = """
CREATE INDEX IF NOT EXISTS idx_shadow_results_suggestion
    ON llm_regex_shadow_results(suggestion_id);
"""


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class RegexShadowHarness:
    """Run a candidate regex against the target-profile corpus and decide
    whether to promote it to ``accepted_strong``."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def select_pending_shadow_tests(self, *, limit: int = 25) -> list[dict[str, Any]]:
        """Return suggestions in ``accepted_synthetic`` that have no shadow run yet."""
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT s.*
                FROM llm_regex_suggestions s
                WHERE s.status = 'accepted_synthetic'
                  AND s.id NOT IN (
                      SELECT DISTINCT suggestion_id FROM llm_regex_shadow_results
                  )
                  AND COALESCE(NULLIF(TRIM(s.target_profile),''), '') != ''
                  AND COALESCE(NULLIF(TRIM(s.candidate_regex),''), '') != ''
                ORDER BY s.confidence DESC, s.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def shadow_test_all_pending(self, *, limit: int = 25) -> list[ShadowAggregate]:
        pending = self.select_pending_shadow_tests(limit=limit)
        results: list[ShadowAggregate] = []
        for row in pending:
            try:
                results.append(self.shadow_test_suggestion(row))
            except Exception as exc:
                logger.warning(
                    "shadow_test_suggestion failed for id=%s: %s",
                    row.get("id"), exc, exc_info=True,
                )
        return results

    def shadow_test_suggestion(self, suggestion_row: dict[str, Any]) -> ShadowAggregate:
        suggestion_id = int(suggestion_row.get("id", 0))
        target_profile = (suggestion_row.get("target_profile") or "").strip()
        candidate_regex = suggestion_row.get("candidate_regex") or ""
        candidate_norm = suggestion_row.get("candidate_normalization") or ""

        agg = ShadowAggregate(suggestion_id=suggestion_id, target_profile=target_profile)

        try:
            pattern = re.compile(candidate_regex, re.IGNORECASE | re.MULTILINE)
        except re.error as e:
            agg.final_status = "rejected_shadow"
            agg.notes = f"regex compile error: {e}"
            self._update_suggestion_status(suggestion_id, "rejected_shadow")
            return agg

        # Stream target-profile corpus
        for doc in self._iter_profile_corpus(target_profile):
            text = self._get_document_text(doc["source_pdf"])
            if not text:
                continue
            before_count = self._count_baseline_matches(text)
            after_text = self._apply_norm(text, candidate_norm)
            after_matches = pattern.findall(after_text)
            after_count = len(after_matches)
            extracted_values = self._extract_numeric_values(after_matches)
            out_of_range = sum(
                1 for v in extracted_values
                if not (SHADOW_VALUE_LOW <= v <= SHADOW_VALUE_HIGH)
            )

            doc_result = ShadowDocResult(
                source_pdf=doc["source_pdf"],
                profile=target_profile,
                doc_status=doc["status"],
                before_count=before_count,
                after_count=after_count,
                extracted_values=extracted_values[:10],
                out_of_range_values=out_of_range,
            )
            delta_kind = self._classify_delta(doc_result)
            self._persist_doc_result(suggestion_id, doc_result, delta_kind)

            agg.docs_tested += 1
            agg.out_of_range_total += out_of_range
            if delta_kind == "improved":
                agg.improved_docs += 1
            elif delta_kind == "regressed":
                agg.regressed_docs += 1

        # Sample unrelated profiles
        unrelated_sample = list(self._iter_unrelated_sample(target_profile))
        agg.unrelated_total = len(unrelated_sample)
        for doc in unrelated_sample:
            text = self._get_document_text(doc["source_pdf"])
            if not text:
                continue
            after_text = self._apply_norm(text, candidate_norm)
            after_count = len(pattern.findall(after_text))
            kind = "unrelated_match" if after_count > 0 else "unrelated_clean"
            self._persist_doc_result(
                suggestion_id,
                ShadowDocResult(
                    source_pdf=doc["source_pdf"],
                    profile=doc.get("parser_profile") or "?",
                    doc_status=doc.get("status") or "?",
                    before_count=0,
                    after_count=after_count,
                ),
                kind,
            )
            if after_count > 0:
                agg.unrelated_match_docs += 1

        # Apply acceptance rules
        agg.final_status, agg.notes = self._decide(agg)
        if agg.final_status:
            self._update_suggestion_status(suggestion_id, agg.final_status)
        return agg

    # ------------------------------------------------------------------
    # Internal — decision rules
    # ------------------------------------------------------------------

    def _decide(self, agg: ShadowAggregate) -> tuple[str, str]:
        fp_rate = (
            agg.unrelated_match_docs / agg.unrelated_total
            if agg.unrelated_total >= SHADOW_MIN_FP_SAMPLE
            else 0.0
        )

        if agg.regressed_docs > SHADOW_MAX_REGRESSED_DOCS:
            return (
                "rejected_shadow",
                f"regression on {agg.regressed_docs} parsed docs",
            )
        if (
            agg.unrelated_total >= SHADOW_MIN_FP_SAMPLE
            and fp_rate > SHADOW_MAX_FP_RATE
        ):
            return (
                "rejected_shadow",
                f"false-positive rate {fp_rate:.1%} on {agg.unrelated_total} unrelated docs",
            )
        if agg.docs_tested > 0 and agg.out_of_range_total > agg.docs_tested * 0.5:
            return (
                "rejected_shadow",
                f"value-range mismatch: {agg.out_of_range_total} of "
                f"{agg.docs_tested} docs produced out-of-range numerics",
            )
        if agg.improved_docs >= SHADOW_MIN_IMPROVED_DOCS:
            return (
                "accepted_strong",
                f"improved {agg.improved_docs} failed docs, "
                f"0 regressions, fp_rate={fp_rate:.1%}",
            )
        # Inconclusive — leave at accepted_synthetic for next run.
        return (
            "",
            f"inconclusive: improved={agg.improved_docs}, "
            f"regressed={agg.regressed_docs}, fp_rate={fp_rate:.1%}",
        )

    def _classify_delta(self, doc: ShadowDocResult) -> str:
        """Classify a per-doc result.

        The candidate regex is an *addition* to the parser, not a replacement.
        So "improvement" = the regex found something on a doc the parser
        couldn't handle (status in empty/failed/partial). "regression" = the
        regex produced numerically implausible values on a parsed doc — that's
        the main way an additive regex can hurt us in production.
        """
        if doc.doc_status in ("empty", "failed", "partial") and doc.after_count > 0:
            return "improved"
        if doc.doc_status == "parsed" and doc.out_of_range_values > 0:
            return "regressed"
        return "unchanged"

    # ------------------------------------------------------------------
    # Internal — corpus iteration
    # ------------------------------------------------------------------

    def _iter_profile_corpus(self, profile: str):
        if not profile:
            return
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT source_pdf, status, charge_count
                FROM parse_attempt_logs
                WHERE parser_profile = ?
                  AND status IN ('parsed', 'empty', 'partial')
                ORDER BY id DESC
                LIMIT ?
                """,
                (profile, SHADOW_PROFILE_CORPUS_CAP),
            ).fetchall()
        finally:
            conn.close()
        for r in rows:
            yield dict(r)

    def _iter_unrelated_sample(self, profile: str, *, sample: int = 60):
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT source_pdf, parser_profile, status
                FROM parse_attempt_logs
                WHERE parser_profile != ?
                  AND parser_profile IS NOT NULL
                  AND parser_profile != ''
                  AND status = 'parsed'
                ORDER BY RANDOM()
                LIMIT ?
                """,
                (profile, sample),
            ).fetchall()
        finally:
            conn.close()
        for r in rows:
            yield dict(r)

    # ------------------------------------------------------------------
    # Internal — text retrieval and counting
    # ------------------------------------------------------------------

    def _get_document_text(self, source_pdf: str) -> str:
        if not source_pdf:
            return ""
        conn = sqlite3.connect(str(self._db_path))
        try:
            pages = conn.execute(
                """
                SELECT text_content
                FROM ncuc_page_artifacts
                WHERE source_pdf = ?
                ORDER BY page_number
                LIMIT 12
                """,
                (source_pdf,),
            ).fetchall()
            if pages:
                return "\n".join(p[0] or "" for p in pages)
        except Exception:
            pass
        finally:
            conn.close()
        return ""

    _BASELINE_RATE = re.compile(
        r"(?:(?:\$\s*)?\d+\.?\d*\s*(?:¢|cents?|c\b|per\s+kWh|per\s+month|/kWh|/kW|/month))",
        re.IGNORECASE,
    )

    def _count_baseline_matches(self, text: str) -> int:
        return len(self._BASELINE_RATE.findall(text))

    def _apply_norm(self, text: str, norm_rule: str) -> str:
        # Mirror regex_validation._apply_normalization minimal behavior — if the
        # rule is a literal find→replace, apply it. Otherwise pass through.
        if not norm_rule:
            return text
        if "->" in norm_rule:
            try:
                left, right = norm_rule.split("->", 1)
                return text.replace(left.strip(), right.strip())
            except Exception:
                return text
        return text

    _NUMERIC_PIECE = re.compile(r"-?\d+(?:\.\d+)?")

    def _extract_numeric_values(self, matches: list[Any]) -> list[float]:
        out: list[float] = []
        for m in matches:
            # findall returns either str or tuple (when there are groups)
            if isinstance(m, tuple):
                pieces = " ".join(p for p in m if isinstance(p, str))
            else:
                pieces = m if isinstance(m, str) else ""
            for v in self._NUMERIC_PIECE.findall(pieces):
                try:
                    out.append(float(v))
                except ValueError:
                    continue
        return out

    # ------------------------------------------------------------------
    # Internal — persistence
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute(_DDL_SHADOW_RESULTS)
            conn.execute(_DDL_SHADOW_INDEX)
            conn.commit()
            conn.close()
        except Exception:
            logger.warning("shadow-results schema bootstrap failed", exc_info=True)

    def _persist_doc_result(
        self,
        suggestion_id: int,
        doc: ShadowDocResult,
        delta_kind: str,
    ) -> None:
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute(
                """
                INSERT INTO llm_regex_shadow_results
                    (suggestion_id, source_pdf, parser_profile, doc_status,
                     before_count, after_count, extracted_values_json,
                     out_of_range_count, delta_kind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    suggestion_id,
                    doc.source_pdf,
                    doc.profile,
                    doc.doc_status,
                    doc.before_count,
                    doc.after_count,
                    json.dumps(doc.extracted_values),
                    doc.out_of_range_values,
                    delta_kind,
                ),
            )
            conn.commit()
            conn.close()
        except Exception:
            logger.warning(
                "failed to persist shadow doc result for suggestion %s",
                suggestion_id, exc_info=True,
            )

    def _update_suggestion_status(self, suggestion_id: int, status: str) -> None:
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute(
                "UPDATE llm_regex_suggestions SET status = ? WHERE id = ?",
                (status, suggestion_id),
            )
            conn.commit()
            conn.close()
        except Exception:
            logger.warning(
                "failed to update suggestion status for %s", suggestion_id, exc_info=True,
            )
