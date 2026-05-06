"""
Deterministic validation harness for regex/normalization suggestions (Phase 5.6).

Evaluates candidate regexes and normalization rules from ``llm_regex_suggestions``
against known successful documents, known failed documents, negative examples, and
unrelated document types. Only marks a suggestion as ``accepted_candidate`` if it:
- Improves the target failed case
- Does not reduce existing successful parses
- Does not introduce broad false positives
- Preserves units and value interpretation

Does NOT modify parser code. Runs regex as a test against extracted text.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3

logger = logging.getLogger(__name__)
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Allowed statuses
# ---------------------------------------------------------------------------

ALLOWED_VALIDATION_STATUSES: tuple[str, ...] = (
    "accepted_candidate",       # passed real-doc improvement + no FPs
    "accepted_synthetic",       # passed LLM test cases at threshold; no real-doc evidence
    "rejected_false_positive",
    "rejected_no_gain",
    "rejected_invalid_regex",
    "needs_human_review",
)

# Synthetic-test acceptance thresholds. A suggestion can graduate to
# accepted_synthetic when its LLM-provided positive/negative test cases pass
# at these rates AND no other real-doc evidence contradicts it.
SYNTHETIC_MIN_POSITIVE_RATE = 0.8
SYNTHETIC_MIN_NEGATIVE_RATE = 0.8
SYNTHETIC_MIN_TOTAL_CASES = 3  # need at least N cases overall to trust the signal


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class RegressionFailure(BaseModel):
    document: str = Field(description="source_pdf that regressed")
    issue: str = Field(description="What went wrong (false positive, lost match, etc.)")
    excerpt: str = Field(default="", description="Relevant text excerpt")


class RegexValidationResult(BaseModel):
    suggestion_id: int
    status: str = Field(default="pending", description=f"One of: {', '.join(ALLOWED_VALIDATION_STATUSES)}")
    before_matched_fields: int = 0
    before_charge_count: int = 0
    after_matched_fields: int = 0
    after_charge_count: int = 0
    regression_failures: list[RegressionFailure] = Field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# Validation harness
# ---------------------------------------------------------------------------


class RegexValidationHarness:
    """Deterministic validation for regex/normalization suggestions.

    Parameters
    ----------
    db_path : Path
        Path to the SQLite database.
    max_test_docs : int
        Maximum unrelated documents to test for false positives (default 20).
    """

    VALIDATION_STAGES: dict[str, tuple[int, int]] = {
        # stage_name: (min_ok_to_pass, max_fail_to_fail)
        "same_profile_success": (3, 0),   # regression: must not break existing successful parses
        "same_profile_failed": (0, 999),   # improvement: should help the failed case
        "unrelated_profiles": (0, 0),      # false-positive: must not match unrelated docs
        "redline_or_proposed": (0, 1),     # boundary: at most 1 false positive on redlines
    }

    def __init__(self, db_path: Path, max_test_docs: int = 20) -> None:
        self._db_path = db_path
        self._max_test_docs = max_test_docs

    def reset_human_review_for_revalidation(
        self, *, limit: int = 100, min_age_minutes: int = 60
    ) -> int:
        """Move stuck ``needs_human_review`` suggestions back into the
        pending-validation queue so they get another pass under the current
        thresholds (which may have tightened or loosened since the prior run).

        Skips suggestions whose most recent validation result is younger than
        ``min_age_minutes`` — running revalidate twice within an hour just
        re-evaluates the same regex against the same docs and produces the
        same verdict, wasting cycles.

        Deletes the prior validation results so the next ``validate`` task
        picks them up. Returns the number of suggestions reset.
        """
        try:
            conn = sqlite3.connect(str(self._db_path))
            ids = [
                r[0] for r in conn.execute(
                    """
                    SELECT s.id
                    FROM llm_regex_suggestions s
                    WHERE s.status = 'needs_human_review'
                      AND NOT EXISTS (
                          SELECT 1 FROM llm_regex_validation_results vr
                          WHERE vr.suggestion_id = s.id
                            AND vr.created_at > datetime('now', ?)
                      )
                    LIMIT ?
                    """,
                    (f'-{int(min_age_minutes)} minutes', limit),
                ).fetchall()
            ]
            if not ids:
                conn.close()
                return 0
            placeholders = ", ".join(["?"] * len(ids))
            conn.execute(
                f"DELETE FROM llm_regex_validation_results WHERE suggestion_id IN ({placeholders})",
                ids,
            )
            conn.execute(
                f"UPDATE llm_regex_suggestions SET status = 'pending_review' "
                f"WHERE id IN ({placeholders})",
                ids,
            )
            conn.commit()
            conn.close()
            return len(ids)
        except Exception:
            logger.warning("reset_human_review_for_revalidation failed", exc_info=True)
            return 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select_pending_suggestions(
        self, *, limit: int = 10, suggestion_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Query ``llm_regex_suggestions`` for pending (not yet validated) suggestions."""
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            params: list[Any] = []
            extra_where = ""
            if suggestion_id:
                extra_where = " AND ls.id = ?"
                params.append(suggestion_id)

            rows = conn.execute(
                f"""
                SELECT ls.*
                FROM llm_regex_suggestions ls
                WHERE ls.status = 'pending_review'
                  AND ls.id NOT IN (
                      SELECT suggestion_id FROM llm_regex_validation_results
                  )
                  {extra_where}
                ORDER BY ls.confidence DESC
                LIMIT ?
                """,
                tuple(params + [limit]),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def validate_suggestion(self, suggestion_row: dict[str, Any]) -> RegexValidationResult:
        """Run the full validation suite on one suggestion.

        Returns a ``RegexValidationResult`` with status and before/after metrics.
        """
        suggestion_id = suggestion_row.get("id", 0)
        suggestion_type = suggestion_row.get("suggestion_type", "")
        target_profile = suggestion_row.get("target_profile") or ""
        candidate_regex = suggestion_row.get("candidate_regex") or ""
        candidate_norm = suggestion_row.get("candidate_normalization") or ""

        # Load positive/negative test cases from the suggestion
        positive_cases: list[dict[str, Any]] = []
        negative_cases: list[dict[str, Any]] = []
        try:
            positive_cases = json.loads(suggestion_row.get("positive_test_cases_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            pass
        try:
            negative_cases = json.loads(suggestion_row.get("negative_test_cases_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            pass

        result = RegexValidationResult(suggestion_id=suggestion_id)

        # Phase 1: Test against positive/negative test cases from the LLM
        case_results = self._validate_test_cases(candidate_regex, candidate_norm, positive_cases, negative_cases)
        result.notes += case_results["notes"]
        if not case_results["regex_valid"]:
            result.status = "rejected_invalid_regex"
            result.notes += " | Rejected: regex failed to compile."
            self._persist_result(result, suggestion_id, suggestion_row)
            self._update_suggestion_status(suggestion_id, result.status)
            return result

        # Phase 2: Test against same-profile successful documents (regression)
        success_docs = self._get_docs_by_profile(target_profile, status="parsed", limit=5)
        regression_failures = []
        before_total = 0
        after_total = 0

        for doc in success_docs:
            text = self._get_document_text(doc.get("source_pdf", ""))
            if not text:
                continue
            before_count = self._count_matches_without(text)
            after_count = self._count_matches_with(text, candidate_regex, candidate_norm)
            before_total += before_count
            after_total += after_count
            if after_count < before_count:
                regression_failures.append(
                    RegressionFailure(
                        document=doc.get("source_pdf", "?"),
                        issue=f"Lost matches: {before_count} → {after_count}",
                        excerpt=text[:200],
                    )
                )

        result.before_matched_fields = before_total
        result.after_matched_fields = after_total

        # Phase 3: Test against same-profile failed documents (improvement)
        failed_docs = self._get_docs_by_profile(target_profile, status="failed", limit=3)
        improved = False
        for doc in failed_docs:
            text = self._get_document_text(doc.get("source_pdf", ""))
            if not text:
                continue
            before_count = self._count_matches_without(text)
            after_count = self._count_matches_with(text, candidate_regex, candidate_norm)
            if after_count > before_count:
                improved = True
                result.before_charge_count = before_count
                result.after_charge_count = after_count

        # Phase 4: Test against unrelated profiles (false positive check)
        unrelated_docs = self._get_unrelated_docs(target_profile, limit=self._max_test_docs)
        fp_count = 0
        for doc in unrelated_docs:
            text = self._get_document_text(doc.get("source_pdf", ""))
            if not text:
                continue
            after_count = self._count_matches_with(text, candidate_regex, candidate_norm)
            if after_count > 0:
                fp_count += 1
                if fp_count <= 3:
                    regression_failures.append(
                        RegressionFailure(
                            document=doc.get("source_pdf", "?"),
                            issue=f"False positive on unrelated doc (profile={doc.get('parser_profile', '?')})",
                            excerpt=text[:200],
                        )
                    )

        result.regression_failures = regression_failures

        # Synthetic-test pass rates (defensive: avoid div-by-zero)
        pos_total = case_results["pos_total"]
        neg_total = case_results["neg_total"]
        total_cases = pos_total + neg_total
        pos_rate = case_results["pos_pass"] / pos_total if pos_total else 0.0
        neg_rate = case_results["neg_pass"] / neg_total if neg_total else 0.0
        synthetic_passes = (
            total_cases >= SYNTHETIC_MIN_TOTAL_CASES
            and pos_rate >= SYNTHETIC_MIN_POSITIVE_RATE
            and neg_rate >= SYNTHETIC_MIN_NEGATIVE_RATE
        )
        # The regex is *too greedy* if it matches almost every negative case
        # the LLM specifically constructed to not match — that's a clear FP signal.
        synthetic_too_greedy = (
            neg_total >= SYNTHETIC_MIN_TOTAL_CASES and neg_rate <= 0.25
        )
        # The regex is *too narrow* if it matches almost no positive case the LLM
        # specifically constructed to match — likely a malformed pattern.
        synthetic_too_narrow = (
            pos_total >= SYNTHETIC_MIN_TOTAL_CASES and pos_rate <= 0.25
        )

        # Determine status — order matters: real-doc evidence beats synthetic.
        if regression_failures and len(regression_failures) > fp_count:
            # Regression on known-good docs
            result.status = "rejected_false_positive"
            result.notes += " | Rejected: causes regression on known-good documents."
        elif fp_count > 3:
            result.status = "rejected_false_positive"
            result.notes += f" | Rejected: {fp_count} false positives on unrelated documents."
        elif not candidate_regex and not candidate_norm:
            result.status = "rejected_no_gain"
            result.notes += " | Rejected: empty suggestion (no regex or normalization)."
        elif improved and not regression_failures and fp_count <= 1:
            # Strong evidence: improves a real failed doc, no regressions, no broad FPs.
            result.status = "accepted_candidate"
            result.notes += " | Accepted: improves target, no regressions, no broad false positives."
        elif not improved and failed_docs:
            # We had failed-doc text and the regex didn't help — real-doc rejection wins.
            result.status = "rejected_no_gain"
            result.notes += " | Rejected: no improvement on target failed documents."
        elif synthetic_too_greedy:
            # Matches almost every negative case the LLM picked — broad false positive risk.
            result.status = "rejected_false_positive"
            result.notes += (
                f" | Rejected (synthetic): negative cases {case_results['neg_pass']}/{neg_total}"
                f" passed — pattern is too greedy."
            )
        elif synthetic_too_narrow:
            # Matches almost no positive case the LLM constructed — malformed.
            result.status = "rejected_no_gain"
            result.notes += (
                f" | Rejected (synthetic): positive cases {case_results['pos_pass']}/{pos_total}"
                f" passed — pattern is too narrow."
            )
        elif synthetic_passes and not regression_failures and fp_count <= 1:
            # No real-doc evidence available, but synthetic test cases pass cleanly
            # and there's no contradicting signal (no regressions, no broad FPs).
            result.status = "accepted_synthetic"
            result.notes += (
                f" | Accepted (synthetic): pos={pos_rate:.0%} neg={neg_rate:.0%}"
                f" over {total_cases} cases; no real-doc evidence available."
            )
        else:
            result.status = "needs_human_review"
            result.notes += " | Needs review: ambiguous validation result."

        # Persist
        self._persist_result(result, suggestion_id, suggestion_row)

        # Update suggestion status
        self._update_suggestion_status(suggestion_id, result.status)

        return result

    def validate_all_pending(self, limit: int = 10) -> list[RegexValidationResult]:
        """Validate all pending suggestions."""
        pending = self.select_pending_suggestions(limit=limit)
        results: list[RegexValidationResult] = []
        for row in pending:
            try:
                result = self.validate_suggestion(row)
                results.append(result)
            except Exception as exc:
                logger.warning(
                    "validate_suggestion failed for id=%s: %s",
                    row.get("id"), exc, exc_info=True,
                )
                continue
        return results

    # ------------------------------------------------------------------
    # Internal — text and document retrieval
    # ------------------------------------------------------------------

    def _get_document_text(self, source_pdf: str) -> str:
        """Get text for a PDF from page artifacts."""
        if not source_pdf:
            return ""
        conn = sqlite3.connect(str(self._db_path))
        try:
            pages = conn.execute(
                """
                SELECT pa.text_content
                FROM ncuc_page_artifacts pa
                WHERE pa.source_pdf = ?
                ORDER BY pa.page_number
                LIMIT 10
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

    def _get_docs_by_profile(
        self, profile: str, status: str = "parsed", limit: int = 5
    ) -> list[dict[str, Any]]:
        """Get documents using a specific parser profile with a given status."""
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT source_pdf, parser_profile, charge_count
                FROM parse_attempt_logs
                WHERE parser_profile = ?
                  AND status = ?
                  AND charge_count > 0
                ORDER BY id DESC
                LIMIT ?
                """,
                (profile, status, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
        finally:
            conn.close()

    def _get_unrelated_docs(self, profile: str, limit: int = 20) -> list[dict[str, Any]]:
        """Get documents using DIFFERENT parser profiles for false-positive testing."""
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT source_pdf, parser_profile
                FROM parse_attempt_logs
                WHERE parser_profile != ?
                  AND parser_profile IS NOT NULL
                  AND parser_profile != ''
                  AND status IN ('parsed', 'failed')
                ORDER BY id DESC
                LIMIT ?
                """,
                (profile, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Internal — matching logic
    # ------------------------------------------------------------------

    def _count_matches_without(self, text: str) -> int:
        """Count existing charge-like matches without the candidate fix.

        This is a heuristic baseline — it counts lines that look like
        existing rate line patterns (dollar amounts, kWh, etc.).
        """
        rate_pattern = re.compile(
            r"(?:(?:\$\s*)?\d+\.?\d*\s*(?:¢|cents?|c\b|per\s+kWh|per\s+month|/kWh|/kW|/month))",
            re.IGNORECASE,
        )
        return len(rate_pattern.findall(text))

    def _count_matches_with(
        self, text: str, candidate_regex: str, candidate_norm: str
    ) -> int:
        """Count matches after applying candidate regex + normalization."""
        working_text = text

        # Apply normalization first
        if candidate_norm:
            working_text = self._apply_normalization(working_text, candidate_norm)

        # Apply candidate regex
        if candidate_regex:
            try:
                pattern = re.compile(candidate_regex, re.IGNORECASE | re.MULTILINE)
                return len(pattern.findall(working_text))
            except re.error:
                return 0

        # No regex — fall back to baseline counting
        return self._count_matches_without(working_text)

    def _apply_normalization(self, text: str, norm_rule: str) -> str:
        """Apply a textual normalization rule.

        The normalization rule should be a simple find/replace description.
        This is a heuristic implementation — real normalization rules
        should be applied by the actual OCR normalization pipeline.
        """
        # Handle common OCR normalization patterns
        if "→" in norm_rule or "=>" in norm_rule or "->" in norm_rule:
            for sep in ("→", "=>", "->"):
                if sep in norm_rule:
                    parts = norm_rule.split(sep, 1)
                    if len(parts) == 2:
                        text = text.replace(parts[0].strip(), parts[1].strip())
                    break
        return text

    def _validate_test_cases(
        self,
        candidate_regex: str,
        candidate_norm: str,
        positive_cases: list[dict[str, Any]],
        negative_cases: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Validate the LLM-provided test cases against the candidate regex.

        Returns a dict with structured counts and a notes string, e.g.::

            {
                "pos_pass": 4, "pos_total": 4,
                "neg_pass": 3, "neg_total": 4,
                "regex_valid": True,
                "notes": "Positive cases: 4/4 passed; Negative cases: 3/4 passed",
            }
        """
        result: dict[str, Any] = {
            "pos_pass": 0, "pos_total": 0,
            "neg_pass": 0, "neg_total": 0,
            "regex_valid": True,
            "notes": "No test cases to validate.",
        }
        if not candidate_regex:
            return result

        notes_parts: list[str] = []
        try:
            pattern = re.compile(candidate_regex, re.IGNORECASE | re.MULTILINE)
        except re.error as e:
            result["regex_valid"] = False
            result["notes"] = f"Invalid regex: {e}"
            return result

        for case in positive_cases:
            text = case.get("text", "")
            should_match = case.get("should_match", True)
            if bool(pattern.search(text)) == should_match:
                result["pos_pass"] += 1
            result["pos_total"] += 1
        notes_parts.append(f"Positive cases: {result['pos_pass']}/{result['pos_total']} passed")

        for case in negative_cases:
            text = case.get("text", "")
            should_match = case.get("should_match", False)
            if bool(pattern.search(text)) == should_match:
                result["neg_pass"] += 1
            result["neg_total"] += 1
        notes_parts.append(f"Negative cases: {result['neg_pass']}/{result['neg_total']} passed")

        result["notes"] = "; ".join(notes_parts)
        return result

    # ------------------------------------------------------------------
    # Internal — persistence
    # ------------------------------------------------------------------

    def _persist_result(
        self,
        result: RegexValidationResult,
        suggestion_id: int,
        suggestion_row: dict[str, Any],
    ) -> None:
        """Write validation result to llm_regex_validation_results."""
        test_ids = []
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute(
                """
                INSERT INTO llm_regex_validation_results
                    (suggestion_id, status, before_matched_fields, before_charge_count,
                     after_matched_fields, after_charge_count, regression_failures_json,
                     test_document_ids_json, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    suggestion_id,
                    result.status,
                    result.before_matched_fields,
                    result.before_charge_count,
                    result.after_matched_fields,
                    result.after_charge_count,
                    json.dumps([rf.model_dump() for rf in result.regression_failures]),
                    json.dumps(test_ids),
                    result.notes[:2000] if result.notes else "",
                ),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def _update_suggestion_status(self, suggestion_id: int, status: str) -> None:
        """Update the suggestion's status in llm_regex_suggestions."""
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute(
                "UPDATE llm_regex_suggestions SET status = ? WHERE id = ?",
                (status, suggestion_id),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass
