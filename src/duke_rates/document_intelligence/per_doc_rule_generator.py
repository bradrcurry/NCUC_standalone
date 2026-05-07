"""
Per-document rule generator (Phase 4B of parsing-architecture refactor).

For Tier 3 docs (low-confidence identity, no template binding) AND for
Tier 1 docs whose template extraction failed, generate regexes attached
to the *specific* document_identity row — never to a profile/template.

Why this exists:
    The legacy ``regex_suggestions.py`` pipeline asks the LLM "give me a
    regex for profile X." That regex then false-positives on every other
    doc in the profile (Phase 0B's anchor-injection mitigated this but
    didn't eliminate it). Per-doc rules carry their own scope, so they
    cannot leak — a regex generated for one doc only ever runs against
    that doc and a small validation sibling set.

Pipeline:
    1. select_candidates()  — pick Tier 3 docs without an accepted rule yet
    2. generate(candidate)  — call LLM with doc-specific anchors + missed text
    3. validate(rule, doc)  — run against target doc + 5 closest siblings;
                              accept only if the rule extracts >=1 charge on
                              the target AND produces no out-of-range numerics
                              on siblings
    4. insert into document_specific_rules with status='accepted' or 'rejected'

Plan reference: ``docs/PARSING_ARCHITECTURE_REFACTOR_PLAN.md`` §7.4B.

Reuses unchanged:
    - The ``RegexSuggestion`` Pydantic schema from regex_suggestions.py
      (the LLM output shape is the same; only the storage destination
      differs).
    - The orchestrator's ``regex_suggestion`` role.
    - The anchor extraction helpers (``fetch_document_anchors``).

Does NOT use:
    - The corpus-wide false-positive sweep from regex_validation.py —
      not relevant when the rule is doc-scoped.
    - The shadow_test harness — same reason.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from duke_rates.document_intelligence.document_specific_rules import (
    ALLOWED_STATUSES,
    DocumentSpecificRule,
    ensure_schema as _rules_ensure_schema,
    insert_rule,
)
from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator
from duke_rates.document_intelligence.regex_suggestions import (
    ALLOWED_RISK_LEVELS,
    ALLOWED_SUGGESTION_TYPES,
    HIGH_SPECIFICITY_CODE_RE,
    RegexSuggestion,
    fetch_document_anchors,
    render_anchors_for_prompt,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# How many closest-sibling docs to test against during validation. The plan
# specifies 5; keeping it tight means a Tier 3 rule can't drift into
# false-positive territory on docs that share signals.
SIBLING_SAMPLE_SIZE = 5

# Plausible per-kWh charge band, matching shadow_test thresholds. Used to
# detect unit-mismatch on sibling docs.
VALUE_LOW = 0.0001
VALUE_HIGH = 1.0

# Minimum count of charges the rule must extract on the *target* doc to be
# considered a real fix. Zero matches → reject; one match is enough since
# Tier 3 docs often have a single rate.
MIN_TARGET_MATCHES = 1


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_PER_DOC_PROMPT = """\
You are a tariff document parsing expert for the North Carolina Utilities Commission (NCUC).
The deterministic parser failed to extract any charges from this single document. Your
task is to write a regex that extracts the rate values from THIS document — not from
any other document in the corpus.

## Document identity:
- Source PDF: {source_pdf}
- Identity confidence: {overall_confidence}
- Schedule codes detected: {schedule_codes}
- Rider codes detected: {rider_codes}
- Distinctive titles: {titles}
- Filename signals: {filename_signals}

## DOCUMENT-SPECIFIC ANCHORS (REQUIRED):
The candidate regex MUST include at least one of these anchors so it does NOT
match unrelated documents in the corpus. A regex without an anchor will be
rejected automatically.

{document_anchors}

## Document text excerpt (first ~1500 chars of rate-relevant content):
```
{document_text}
```

## Instructions:
1. suggestion_type MUST be one of: {allowed_types}
2. risk MUST be one of: {allowed_risks}
3. The candidate_regex MUST contain at least one of the document anchors above.
   A regex like `\\$\\d+\\.\\d+\\s*/kWh` is too generic — scope it, e.g.
   `Schedule\\s+RES-28[\\s\\S]*?\\$(\\d+\\.\\d+)\\s*/kWh`.
4. Provide 2-5 positive_test_cases (lines from THIS document that SHOULD match)
   and 2-5 negative_test_cases (lines that should NOT match — include at least
   one line from a DIFFERENT schedule code as a negative case).
5. expected_unit should be the physical or billing unit (kWh, $/kWh, $/month, etc.)
   or empty if unknown.
6. Confidence 0.0-1.0 based on how specific and evidence-backed the regex is.
7. Return an EMPTY candidate_regex if no plausible pattern exists for this doc —
   do not invent regexes.

Respond with a single JSON object matching the required schema. No other text."""


# ---------------------------------------------------------------------------
# Validation result types
# ---------------------------------------------------------------------------


@dataclass
class PerDocValidationResult:
    """Outcome of validating a candidate regex against one target + siblings."""

    target_matches: int = 0
    target_text_present: bool = False
    siblings_tested: int = 0
    sibling_match_total: int = 0
    sibling_out_of_range_total: int = 0
    accept: bool = False
    reason: str = ""


@dataclass
class GenerationOutcome:
    """End-to-end outcome of generate-and-validate for one Tier 3 doc."""

    source_pdf: str
    document_identity_id: int
    suggestion: RegexSuggestion | None = None
    validation: PerDocValidationResult | None = None
    rule_id: int | None = None
    status: str = "skipped"  # skipped | generated | accepted | rejected | error
    error: str = ""

    def to_summary(self) -> dict[str, Any]:
        return {
            "source_pdf": self.source_pdf,
            "document_identity_id": self.document_identity_id,
            "status": self.status,
            "rule_id": self.rule_id,
            "target_matches": self.validation.target_matches if self.validation else None,
            "siblings_tested": self.validation.siblings_tested if self.validation else None,
            "sibling_out_of_range": self.validation.sibling_out_of_range_total if self.validation else None,
            "reason": self.validation.reason if self.validation else self.error,
        }


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class PerDocRuleGenerator:
    """Generates and validates document-specific rules for Tier 3 docs."""

    def __init__(
        self,
        orchestrator: OllamaOrchestrator,
        db_path: Path | str,
        *,
        role: str = "regex_suggestion",
    ) -> None:
        self._orch = orchestrator
        self._db_path = Path(db_path)
        self._role = role
        _rules_ensure_schema(self._db_path)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def select_candidates(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """Pick Tier 3 docs that need a rule.

        Selection rules:
          - tier = 3 in document_routing_tier
          - the doc has at least one fingerprint signal (otherwise we
            can't anchor a regex)
          - no accepted document_specific_rule exists for this doc yet
          - the doc has parse text we can validate against (i.e. there's
            a row in ncuc_page_artifacts)

        Ordered by identity confidence descending — high-confidence Tier 3
        docs (those that fell short of Tier 2 only because they lack
        consensus) get first pass.
        """
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT di.id AS document_identity_id,
                       di.source_pdf,
                       di.overall_confidence,
                       di.schedule_codes_strong_json,
                       di.rider_codes_strong_json,
                       di.detected_titles_json,
                       di.filename_signals_json,
                       di.profile_consensus_top
                FROM document_identity di
                JOIN document_routing_tier rt ON rt.source_pdf = di.source_pdf
                WHERE rt.tier = 3
                  AND (
                    di.schedule_codes_strong_json != '[]'
                    OR di.detected_titles_json != '[]'
                    OR di.filename_signals_json != '[]'
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM document_specific_rules dsr
                    WHERE dsr.document_identity_id = di.id
                      AND dsr.status IN ('accepted', 'pending')
                  )
                  AND EXISTS (
                    SELECT 1 FROM ncuc_page_artifacts pa
                    WHERE pa.source_pdf = di.source_pdf
                  )
                ORDER BY di.overall_confidence DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def generate_for_document(
        self, candidate: dict[str, Any]
    ) -> GenerationOutcome:
        """Run the full generate→validate→persist cycle for one doc.

        Never raises; returns the outcome with status='error' on
        unexpected failures so the caller can keep going through a batch.
        """
        outcome = GenerationOutcome(
            source_pdf=candidate.get("source_pdf") or "",
            document_identity_id=int(candidate.get("document_identity_id") or 0),
        )
        try:
            suggestion = self._call_llm(candidate)
        except Exception as exc:
            outcome.status = "error"
            outcome.error = f"LLM call failed: {exc}"
            return outcome
        if suggestion is None:
            outcome.status = "skipped"
            outcome.error = "LLM returned no usable suggestion"
            return outcome
        outcome.suggestion = suggestion
        outcome.status = "generated"

        if not (suggestion.candidate_regex or "").strip():
            outcome.status = "skipped"
            outcome.error = "LLM returned empty candidate_regex"
            return outcome

        try:
            validation = self.validate(suggestion, candidate)
        except Exception as exc:
            outcome.status = "error"
            outcome.error = f"validation failed: {exc}"
            return outcome
        outcome.validation = validation

        rule_status = "accepted" if validation.accept else "rejected"
        try:
            rule_id = self._persist_rule(suggestion, candidate, rule_status, validation)
            outcome.rule_id = rule_id
        except Exception as exc:
            outcome.status = "error"
            outcome.error = f"persist failed: {exc}"
            return outcome

        outcome.status = rule_status
        return outcome

    def generate_batch(self, *, limit: int = 10) -> list[GenerationOutcome]:
        candidates = self.select_candidates(limit=limit)
        outcomes: list[GenerationOutcome] = []
        for c in candidates:
            outcome = self.generate_for_document(c)
            outcomes.append(outcome)
        return outcomes

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(
        self,
        suggestion: RegexSuggestion,
        candidate: dict[str, Any],
    ) -> PerDocValidationResult:
        """Run the per-doc validation suite.

        Decision logic (plan §7.4B):
          - Compile the regex; reject on regex error.
          - Run against target doc text → must match >= MIN_TARGET_MATCHES.
          - Run against up to SIBLING_SAMPLE_SIZE siblings (closest by
            identity similarity); siblings may match or not, but their
            extracted numerics must stay within VALUE_LOW..VALUE_HIGH.
          - Accept iff target matches >= MIN_TARGET_MATCHES AND siblings
            produced no out-of-range numerics.
        """
        result = PerDocValidationResult()

        regex_str = suggestion.candidate_regex or ""
        try:
            pattern = re.compile(regex_str, re.IGNORECASE | re.MULTILINE)
        except re.error as e:
            result.reason = f"regex compile error: {e}"
            return result

        target_text = self._get_document_text(candidate.get("source_pdf") or "")
        result.target_text_present = bool(target_text)
        if not target_text:
            result.reason = "target doc has no extractable text"
            return result
        target_matches = pattern.findall(target_text)
        result.target_matches = len(target_matches)
        if result.target_matches < MIN_TARGET_MATCHES:
            result.reason = (
                f"target produced {result.target_matches} matches "
                f"(need >= {MIN_TARGET_MATCHES})"
            )
            return result

        # Find sibling docs and run the regex against each.
        siblings = self._select_siblings(candidate, limit=SIBLING_SAMPLE_SIZE)
        result.siblings_tested = len(siblings)
        for sib in siblings:
            sib_text = self._get_document_text(sib["source_pdf"])
            if not sib_text:
                continue
            matches = pattern.findall(sib_text)
            result.sibling_match_total += len(matches)
            for v in self._extract_numeric_values(matches):
                if not (VALUE_LOW <= v <= VALUE_HIGH):
                    result.sibling_out_of_range_total += 1

        if result.sibling_out_of_range_total > 0:
            result.reason = (
                f"sibling validation produced {result.sibling_out_of_range_total} "
                f"out-of-range numeric values"
            )
            return result

        result.accept = True
        result.reason = (
            f"target matched {result.target_matches} times; "
            f"siblings clean ({result.sibling_match_total} matches across "
            f"{result.siblings_tested} docs)"
        )
        return result

    # ------------------------------------------------------------------
    # Internal — LLM call
    # ------------------------------------------------------------------

    def _call_llm(self, candidate: dict[str, Any]) -> RegexSuggestion | None:
        source_pdf = candidate.get("source_pdf") or ""
        anchors = fetch_document_anchors(self._db_path, source_pdf)
        document_anchors = render_anchors_for_prompt(anchors)
        document_text = self._get_document_text(source_pdf, max_pages=8)
        if not document_text:
            logger.info("skipping %s — no document text", source_pdf)
            return None

        # Decode JSON-stored signal lists for the prompt.
        def _load_list(j: str | None) -> list[str]:
            try:
                data = json.loads(j or "[]")
            except (json.JSONDecodeError, TypeError):
                return []
            return [s for s in data if isinstance(s, str)]

        prompt = _PER_DOC_PROMPT.format(
            source_pdf=source_pdf,
            overall_confidence=candidate.get("overall_confidence") or 0.0,
            schedule_codes=", ".join(_load_list(candidate.get("schedule_codes_strong_json"))) or "(none)",
            rider_codes=", ".join(_load_list(candidate.get("rider_codes_strong_json"))) or "(none)",
            titles=", ".join(_load_list(candidate.get("detected_titles_json"))[:5]) or "(none)",
            filename_signals=", ".join(_load_list(candidate.get("filename_signals_json"))) or "(none)",
            document_anchors=document_anchors,
            document_text=document_text[:1500],
            allowed_types=", ".join(ALLOWED_SUGGESTION_TYPES),
            allowed_risks=", ".join(ALLOWED_RISK_LEVELS),
        )
        run_result = self._orch.generate_json(
            role=self._role,
            prompt=prompt,
            schema=RegexSuggestion,
            subject_kind="document_identity",
            subject_id=str(candidate.get("document_identity_id") or 0),
            stage="per_doc_rule",
        )
        if run_result.status not in ("ok", "fallback_used"):
            return None
        suggestion: RegexSuggestion = run_result.result

        # Defensive normalization
        if suggestion.suggestion_type not in ALLOWED_SUGGESTION_TYPES:
            suggestion.suggestion_type = "regex_candidate"
        if suggestion.risk not in ALLOWED_RISK_LEVELS:
            suggestion.risk = "medium"
        return suggestion

    # ------------------------------------------------------------------
    # Internal — sibling selection (Jaccard on signals)
    # ------------------------------------------------------------------

    def _select_siblings(
        self, candidate: dict[str, Any], *, limit: int
    ) -> list[dict[str, Any]]:
        """Pick the docs most similar to the target by signal overlap.

        Cheap Jaccard over schedule_codes ∪ rider_codes ∪ filename_signals.
        Excludes the target itself.
        """
        target_id = int(candidate.get("document_identity_id") or 0)
        target_signals = self._extract_signal_set(candidate)
        if not target_signals:
            return []

        # Pull all rows once and score in Python — corpus is small enough.
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id AS document_identity_id, source_pdf,
                       schedule_codes_strong_json, rider_codes_strong_json,
                       filename_signals_json
                FROM document_identity
                WHERE id != ?
                """,
                (target_id,),
            ).fetchall()
        finally:
            conn.close()

        scored: list[tuple[float, dict[str, Any]]] = []
        for r in rows:
            other_signals = self._extract_signal_set(dict(r))
            if not other_signals:
                continue
            inter = len(target_signals & other_signals)
            if inter == 0:
                continue
            union = len(target_signals | other_signals)
            jaccard = inter / union
            scored.append((jaccard, dict(r)))
        scored.sort(key=lambda kv: kv[0], reverse=True)
        return [d for _, d in scored[:limit]]

    @staticmethod
    def _extract_signal_set(row: dict[str, Any]) -> set[str]:
        out: set[str] = set()
        for key in (
            "schedule_codes_strong_json",
            "rider_codes_strong_json",
            "filename_signals_json",
        ):
            try:
                vals = json.loads(row.get(key) or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            for v in vals:
                if isinstance(v, str) and v.strip():
                    out.add(v.strip().upper())
        return out

    # ------------------------------------------------------------------
    # Internal — text retrieval & numeric extraction
    # ------------------------------------------------------------------

    def _get_document_text(self, source_pdf: str, *, max_pages: int = 12) -> str:
        if not source_pdf:
            return ""
        conn = sqlite3.connect(str(self._db_path))
        try:
            rows = conn.execute(
                """
                SELECT text_content
                FROM ncuc_page_artifacts
                WHERE source_pdf = ?
                ORDER BY page_number
                LIMIT ?
                """,
                (source_pdf, max_pages),
            ).fetchall()
        except Exception:
            return ""
        finally:
            conn.close()
        return "\n".join((r[0] or "") for r in rows)

    _NUMERIC = re.compile(r"-?\d+(?:\.\d+)?")

    def _extract_numeric_values(self, matches: list[Any]) -> list[float]:
        out: list[float] = []
        for m in matches:
            if isinstance(m, tuple):
                pieces = " ".join(p for p in m if isinstance(p, str))
            elif isinstance(m, str):
                pieces = m
            else:
                continue
            for v in self._NUMERIC.findall(pieces):
                try:
                    out.append(float(v))
                except ValueError:
                    continue
        return out

    # ------------------------------------------------------------------
    # Internal — persistence
    # ------------------------------------------------------------------

    def _persist_rule(
        self,
        suggestion: RegexSuggestion,
        candidate: dict[str, Any],
        status: str,
        validation: PerDocValidationResult,
    ) -> int:
        """Write the rule to document_specific_rules and return its id."""
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"persist_rule got bad status: {status!r}")
        rule = DocumentSpecificRule(
            document_identity_id=int(candidate.get("document_identity_id") or 0),
            candidate_regex=suggestion.candidate_regex or "",
            candidate_normalization=(suggestion.candidate_normalization or None) or None,
            expected_unit=(suggestion.expected_unit or None) or None,
            target_field=(suggestion.target_field or None) or None,
            status=status,
            notes=(
                f"per-doc rule generated by Phase 4B; "
                f"validation: {validation.reason}"
            ),
        )
        return insert_rule(self._db_path, rule)
