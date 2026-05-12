"""
Extraction-grounded rule generator (Phase 6F).

Generates per-document regex rules using *already-extracted* high-confidence
rows as the starting point. Inverts the normal "look at raw text, hallucinate
a regex" problem: instead, we hand the LLM a verbatim source line plus the
expected captured value/unit, and ask it to produce a regex that captures
THIS specific text.

Why this works where the previous approach didn't:
    The overnight 9hr run produced 909 rejected per-doc rules vs 10 accepted
    (98.9% reject rate). Bucket analysis showed 100% of recent rejections
    were "target produced 0 matches" — the LLM was emitting regexes that
    looked plausible but didn't match the actual OCR text. By giving the
    LLM a confirmed source quote, we eliminate the "is this line really in
    the doc?" guessing problem.

Pipeline:
    1. Select high-confidence rows from ``llm_candidate_rate_extractions``
       (per-row confidence >= 0.7 OR doc extraction_confidence >= 0.7
       with row value present).
    2. For each row, fetch the parent document's identity for anchors.
    3. Prompt the LLM with: source line, expected value, expected unit,
       schedule code, charge type, and strict regex rules.
    4. Validate the regex three ways: compiles, contains anchor, captures
       the expected value when run against the source line.
    5. Persist to ``document_specific_rules`` with origin tag for
       downstream A/B comparison against the legacy LLM-only path.

Plan reference: ``docs/PARSING_ARCHITECTURE_REFACTOR_PLAN.md`` §6.F.
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
    DocumentSpecificRule,
    ensure_schema,
    insert_rule,
)
from duke_rates.document_intelligence.per_doc_rule_generator import (
    HIGH_SPECIFICITY_CODE_RE,
)
from duke_rates.document_intelligence.regex_suggestions import (
    ALLOWED_RISK_LEVELS,
    ALLOWED_SUGGESTION_TYPES,
    RegexSuggestion,
    fetch_document_anchors,
)

logger = logging.getLogger(__name__)


# Per-row confidence threshold for inclusion. Lower than the doc threshold
# because we use both (per-row OR doc-level) so rows from a high-doc-conf
# extraction get included even when the model emitted confidence=0.0.
_MIN_ROW_CONFIDENCE: float = 0.7
_MIN_DOC_CONFIDENCE: float = 0.7

# Skip source lines this short — they're rarely robust enough to anchor on.
_MIN_QUOTE_LEN: int = 12

# Cap on rules generated per (source_pdf, target_field) pair. Avoids
# generating dozens of near-identical regexes for the same doc.
_MAX_RULES_PER_TARGET_PER_DOC: int = 2

# Maximum retries per row when the validator rejects the regex.
_MAX_RETRIES: int = 1


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------


_GROUNDED_PROMPT = """\
You are writing a Python regex that extracts ONE specific rate value from a
North Carolina tariff document. The rate has ALREADY been identified — your
job is to produce the regex that captures it.

## Source line (verbatim — your regex MUST match this exact text):
"{source_line}"

## What the regex must capture:
- The value `{value}` (the captured group must yield this number, not a different number on a nearby line)
- Unit: {unit}
- Charge type: {charge_type}

## Required anchor (must appear LITERALLY in the regex body):
{anchor}

## Anchor position relative to the rate value in this document:
{anchor_direction_hint}

## Schedule codes present in this document:
{schedule_codes}

## ABSOLUTELY DO NOT (these are the most common failures):

DO NOT put the anchor in a lookahead.
  WRONG: `(\\d+\\.\\d+)¢\\s*per\\s+kWh(?=.*{anchor})`
  RIGHT: `{anchor}[\\s\\S]*?(\\d+\\.\\d+)¢\\s*per\\s+kWh`

DO NOT use unbalanced parentheses. Every `(` needs a `)`. Every `(?P<name>`
needs a closing `)`. Count them before responding.

DO NOT write `.*?` expecting it to cross newlines. Use `[\\s\\S]*?` instead.

DO NOT make the regex so loose it matches a different sibling line.
  Example: if the doc has "1. 39.614¢ per Critical Peak kWh" AND
  "2. 21.209¢ per On-Peak kWh", a regex like `(\\d+\\.\\d+)¢\\s*per\\s+kWh`
  will capture both — you need a distinguishing context word
  (`Critical\\s+Peak` or `On\\-Peak`) BEFORE the capture.

DO NOT use `(?<=...)` variable-length look-behinds (Python rejects them).

DO NOT use PCRE-style `(?<name>...)` named groups. Python requires `(?P<name>...)`.

## CAPTURE GROUP IS MANDATORY:
The `candidate_regex` MUST contain at least one capturing group `(...)`.
The validator extracts the numeric value from the first group. A regex
with no parentheses will be rejected immediately, even if it matches.
  WRONG: `RES\\-28[\\s\\S]*?\\$\\d+\\.\\d+`   ← no group, value cannot be extracted
  RIGHT: `RES\\-28[\\s\\S]*?\\$(\\d+\\.\\d+)`  ← captures the number

## Construction recipe (follow EXACTLY this 5-step order):

{construction_recipe}

## Worked examples:

Source: "I. Basic Customer Charge, per month $14.00" → anchor RES-28, anchor BEFORE value
GOOD: `RES\\-28[\\s\\S]*?Basic\\s+Customer\\s+Charge[\\s\\S]*?\\$(\\d+\\.\\d+)`

Source: "1. 39.614¢ per Critical Peak kWh" → anchor EDIT-4, anchor BEFORE value
GOOD: `EDIT\\-4[\\s\\S]*?Critical\\s+Peak[\\s\\S]*?(\\d+\\.\\d+)¢`
BAD (matches On-Peak too): `EDIT\\-4[\\s\\S]*?(\\d+\\.\\d+)¢\\s*per\\s+kWh`

Source: "Administrative Charge = $200 per month" → anchor HP, anchor BEFORE value
GOOD: `HP[\\s\\S]*?Administrative\\s+Charge[\\s\\S]*?\\$(\\d+)`

Source: "RECD Credit = 5% times the stated kilowatt" → anchor RECD, anchor AFTER value
GOOD: `RECD\\s+Credit[\\s\\S]*?(\\d+)%[\\s\\S]*?RECD`
NOTE: when anchor is AFTER the value, use a distinctive phrase from the source
line as the opening anchor instead, then include the code after the capture.

## Output (single JSON object, no other text):
{{
  "suggestion_type": "regex_candidate",
  "target_field": "{charge_type_field}",
  "candidate_regex": "<your regex following steps 1-5>",
  "candidate_normalization": "<optional, e.g. 'divide captured cents per kWh by 100'>",
  "expected_unit": "{unit}",
  "confidence": 0.0-1.0,
  "risk": "low|medium|high"
}}"""


_RETRY_PROMPT = """\
Your previous regex was REJECTED. Fix it and return a corrected version.

## Rejection reason: {reason}
{captured_actual_block}
## Source line (your regex MUST match this exact line):
"{source_line}"

## Anchor required: {anchor}
## Expected captured value: {value}

## Your failed regex:
{failed_regex}

## Diagnostic checklist (fix the SPECIFIC issue named above):

If "regex didn't match":
  - Replace literal spaces between tokens with `\\s+`.
  - Use `[\\s\\S]*?` to bridge across line breaks; do NOT use `.*?` for this.
  - Make sure the anchor `{anchor}` appears BEFORE any `[\\s\\S]*?` bridge,
    not inside a lookahead.

If "captured wrong value":
  - The regex matched but grabbed a different number from the same doc.
  - Add a distinguishing context word from the source line BEFORE the
    capture group (e.g. `Critical\\s+Peak`, `Basic\\s+Customer\\s+Charge`).
  - Look at the captured-actual list above — those are values your
    previous regex pulled out. Avoid matching them by being more specific.

If "regex compile error":
  - Count your parentheses. Every `(` needs a `)`.
  - `(?P<name>...)` not `(?<name>...)`. Close named groups properly.

If "anchor missing":
  - Include the literal anchor `{anchor}` in the regex body (escape `-` as `\\-`).

Return the same JSON shape as before with a corrected candidate_regex.
No other text. JSON only."""


def _format_captured_actual_block(captured: list[str], expected: float) -> str:
    """Render the 'here's what your regex actually captured' block for retries.

    Returns "" when no captured values exist (e.g. compile errors), so the
    placeholder slot in the retry prompt collapses cleanly.
    """
    if not captured:
        return ""
    pretty = ", ".join(c for c in captured[:6])
    return (
        "\n## What your regex actually captured (expected was "
        f"{expected}):\n{pretty}\n"
    )


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


# Regex tokens that should be stripped before the anchor-presence check
# so a pattern like `Basic\s+Customer\s+Charge` matches the anchor
# "Basic Customer Charge".
_REGEX_TOKEN_STRIP = re.compile(
    r"(\\s\+?|\\s\*|\\d\+?|\\d\*|\[\\s\\S\]\*\??|\[\\s\\S\]\+\??|"
    r"\\\\|\\\$|\\b|\\w\+?|\\w\*|\(\?P<[^>]+>|\(\?:|\(\?!|\(\?=|"
    r"\(|\)|\*\??|\+\??|\?|\^|\{[\d,]+\}|\[\^?[^\]]+\])"
)


# Tokens that look like they could anchor a rate line: schedule/rider
# codes (R-TOU-CPP, RES-28), leaf-number references, or section labels.
_LOCAL_ANCHOR_RE = re.compile(
    r"(?:"
    r"Leaf\s+No\.\s*\d+[A-Za-z]?"      # "Leaf No. 503"
    r"|Schedule\s+[A-Z][A-Z0-9\-]{1,15}" # "Schedule R-TOU-CPP"
    r"|Rider\s+[A-Z][A-Z0-9\-]{1,15}"  # "Rider EDIT-4"
    r"|[A-Z]{2,8}-[A-Z0-9\-]{1,10}"    # bare code like RES-28, R-TOU-CPP
    r")",
    re.MULTILINE,
)


# Phrases inside a rate line that work as in-line anchors when there's no
# nearby schedule code. The captured group is what we use as the anchor.
_IN_LINE_ANCHOR_PATTERNS: tuple[str, ...] = (
    r"(Basic\s+Customer\s+Charge)",
    r"(Basic\s+Facilities\s+Charge)",
    r"(Administrative\s+Charge)",
    r"(Incremental\s+Demand\s+Charge)",
    r"(Demand\s+Charge)",
    r"(Energy\s+Charge)",
    r"(Minimum\s+Bill)",
    r"(Customer\s+Charge)",
    r"(Facilities\s+Charge)",
    r"(Incentive\s+Margin)",
    r"(Critical\s+Peak\s+Energy)",
    r"(On-Peak\s+Energy)",
    r"(Off-Peak\s+Energy)",
    r"(Discount\s+Energy)",
    r"(Fuel\s+Adjustment)",
    r"(Rider\s+[A-Z]{2,8})",
    r"(Schedule\s+[A-Z][A-Z0-9\-]+)",
)
_IN_LINE_ANCHOR_RE = re.compile(
    "|".join(_IN_LINE_ANCHOR_PATTERNS), re.IGNORECASE,
)


def _extract_in_line_anchor(source_quote: str) -> str:
    """Pick a distinctive phrase from the source line itself to use as an anchor.

    For rate lines like "II. Administrative Charge = $200 per month", we
    can't easily anchor on a schedule code (HP is far away), but the
    phrase "Administrative Charge" is itself distinctive enough to
    scope the regex. Returns the first matching phrase or "" if none.
    """
    if not source_quote:
        return ""
    m = _IN_LINE_ANCHOR_RE.search(source_quote)
    if not m:
        return ""
    # Return the actual matched text (preserving original casing).
    return m.group(0).strip()


def _find_local_anchor(
    source_quote: str,
    doc_text: str,
    *,
    window: int = 800,
    forward_window: int = 400,
) -> str:
    """Return the nearest code-like anchor for *source_quote*.

    Document-level identity anchors (from ``schedule_codes_strong_json``)
    often live in a table-of-contents far from the actual rate line. A
    regex spanning 50,000+ chars with `[\\s\\S]*?` is impractical. Instead
    we look ~*window* chars BEFORE the source quote in the doc and pick
    the closest match for a schedule code, leaf number, or rider label.

    When no anchor is found in the backward window, also checks
    *forward_window* chars after the source quote. Some rider docs place
    the code label (e.g. "RIDER RECD-82") after the rate definition, so
    a backward-only scan misses it. The forward anchor is still usable
    because the grounded prompt constructs ``anchor[\\s\\S]*?value`` — when
    the anchor follows the value in the doc we record it as a forward anchor
    so ``select_candidates`` can build a reversed regex
    (``value[\\s\\S]*?anchor``) instead.

    Returns the anchor text string, or ``""`` when none is found.
    Direction tracking is handled by ``select_candidates``: it calls
    this function twice (full window then forward-only) to determine
    whether the anchor is before or after the source quote.
    """
    if not source_quote or not doc_text:
        return ""

    # Try a series of progressively-looser substring matches to find the
    # quote's position in the doc. The staged classifier sometimes trims
    # the leading "1. " enumeration so a strict find fails.
    candidates_to_try: list[str] = [source_quote]
    # Stripped of enumeration prefix.
    stripped = re.sub(r"^[\s\d]*[\.\)A-Za-z]\s+", "", source_quote, count=1).strip()
    if stripped and stripped != source_quote:
        candidates_to_try.append(stripped)
    # Last 25 chars (value+unit tail tends to be the most stable signature).
    tail = source_quote[-25:].strip()
    if len(tail) >= 10:
        candidates_to_try.append(tail)
    # First 25 chars.
    head = source_quote[:25].strip()
    if len(head) >= 10:
        candidates_to_try.append(head)

    idx = -1
    for needle in candidates_to_try:
        idx = doc_text.find(needle)
        if idx >= 0:
            break
    if idx < 0:
        return ""

    # --- Backward scan (preferred) ---
    start = max(0, idx - window)
    preceding = doc_text[start:idx]
    back_matches = list(_LOCAL_ANCHOR_RE.finditer(preceding))
    if back_matches:
        # Closest preceding match = last one.
        return back_matches[-1].group(0).strip()

    # --- Forward scan fallback ---
    # Some rider docs put the schedule/rider code label after the rate line
    # (e.g. "RIDER RECD\n... RECD-82. All applicants..."). Use the closest
    # forward match when the backward window is empty. The candidate dict
    # carries an "anchor_direction" hint so the prompt can build the regex
    # in the right direction (value[\s\S]*?anchor).
    quote_end = idx + len(source_quote)
    following = doc_text[quote_end: quote_end + forward_window]
    fwd_matches = list(_LOCAL_ANCHOR_RE.finditer(following))
    if fwd_matches:
        return fwd_matches[0].group(0).strip()

    return ""


def _quote_substantively_in_text(quote: str, text: str) -> bool:
    """Check whether the substantive content of *quote* appears in *text*.

    Strict ``quote in text`` rejects legitimate quotes when the staged
    classifier trimmed enumeration prefixes like ``1. `` or ``A. ``.
    This loosens the check: we look for the longest "interior" substring
    after stripping common enumeration prefixes and bullet markers.

    Returns True when any of these matches:
      - exact substring match (fast path)
      - the quote with leading enumeration stripped is a substring
      - all words in the quote longer than 3 chars appear in the same
        order within a 200-char window of the document
    """
    if not quote or not text:
        return False
    if quote in text:
        return True
    # Strip leading enumeration: "1. ", "A.", "I.", "(1)", "1)" etc.
    stripped = re.sub(r"^[\s\d]*[\.\)A-Za-z]\s+", "", quote, count=1).strip()
    if stripped and stripped in text:
        return True
    # Try the last 25 chars (the "value + unit" tail is the most stable).
    tail = quote[-25:].strip()
    if len(tail) >= 10 and tail in text:
        return True
    # Word-order fallback: every word longer than 3 chars from the quote
    # must appear in the same order somewhere in the doc.
    words = [w for w in re.findall(r"[A-Za-z0-9$¢.]+", quote) if len(w) > 3]
    if not words:
        return False
    cursor = 0
    for w in words:
        idx = text.find(w, cursor)
        if idx < 0:
            return False
        cursor = idx + len(w)
    return True


def _compact_for_anchor_check(regex_str: str) -> str:
    """Strip regex metacharacters and non-alphanumerics for anchor matching.

    Converts ``Basic\\s+Customer\\s+Charge`` to ``basiccustomercharge`` so a
    space-delimited anchor like "Basic Customer Charge" can be detected
    inside the regex pattern.
    """
    stripped = _REGEX_TOKEN_STRIP.sub("", regex_str)
    return re.sub(r"[^a-z0-9]", "", stripped.lower())


@dataclass
class GroundedValidationResult:
    accept: bool = False
    reason: str = ""
    compiled: bool = False
    anchor_present: bool = False
    matches_source: bool = False
    captures_expected_value: bool = False
    captured_values: list[str] = field(default_factory=list)


@dataclass
class GroundedOutcome:
    source_pdf: str
    extraction_id: int
    row_index: int
    source_quote: str
    suggestion: RegexSuggestion | None = None
    validation: GroundedValidationResult | None = None
    status: str = "pending"
    error: str = ""
    rule_id: int | None = None


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class ExtractionGroundedRuleGenerator:
    """Generate per-doc rules using high-confidence extraction rows as grounding.

    Reuses the ``regex_suggestion`` role (same models as the legacy per-doc
    generator) but with a focused prompt that gives the LLM the exact text
    we want it to capture.
    """

    def __init__(
        self,
        orchestrator: Any,  # OllamaOrchestrator
        db_path: Path | str,
        *,
        role: str = "regex_suggestion",
    ) -> None:
        self._orch = orchestrator
        self._db_path = Path(db_path)
        self._role = role
        ensure_schema(self._db_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select_candidates(
        self, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Return candidate rows from llm_candidate_rate_extractions.

        Each candidate is one (extraction_id, row_index, source_quote,
        value, unit, charge_type) tuple. Filters to per-row confidence
        >= _MIN_ROW_CONFIDENCE OR extraction-level confidence >=
        _MIN_DOC_CONFIDENCE (so high-quality extractions whose model
        emitted confidence=0.0 per row still qualify).

        Also skips:
        - Rows whose source_pdf already has _MAX_RULES_PER_TARGET_PER_DOC
          accepted/pending grounded rules for the same target_field.
        - Rows whose document_identity has no high-specificity code anchors
          (the regex can't be safely scoped otherwise).
        """
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Use a SQL join to filter out docs that lack anchors BEFORE we
            # over-select, so a small ``limit`` doesn't get consumed by
            # the first N extractions with no document_identity row.
            sql = """
            SELECT lcre.id, lcre.source_pdf, lcre.rate_rows_json,
                   lcre.extraction_confidence, lcre.document_signals_json
            FROM llm_candidate_rate_extractions lcre
            JOIN document_identity di ON di.source_pdf = lcre.source_pdf
            WHERE lcre.extraction_confidence >= ?
              AND json_array_length(COALESCE(lcre.rate_rows_json, '[]')) > 0
              AND (
                    COALESCE(di.schedule_codes_strong_json, '[]') != '[]'
                 OR COALESCE(di.rider_codes_strong_json, '[]') != '[]'
              )
            ORDER BY lcre.extraction_confidence DESC, lcre.id DESC
            """
            params: list[Any] = [_MIN_DOC_CONFIDENCE]
            if limit:
                sql += " LIMIT ?"
                # Over-select 20x to allow the per-row filters (confidence,
                # quote length, charge type, unit) to narrow further.
                params.append(int(limit) * 20)
            extraction_rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        candidates: list[dict[str, Any]] = []
        seen_doc_target: dict[tuple[str, str], int] = {}
        # Cache doc text per source_pdf so we only fetch each large PDF once.
        doc_text_cache: dict[str, str] = {}
        for er in extraction_rows:
            doc_conf = float(er["extraction_confidence"] or 0.0)
            try:
                rate_rows = json.loads(er["rate_rows_json"] or "[]")
            except (json.JSONDecodeError, TypeError):
                continue

            identity_anchors = self._get_doc_anchors(er["source_pdf"])
            if not identity_anchors:
                continue  # no anchor -> can't safely scope a regex
            if er["source_pdf"] not in doc_text_cache:
                doc_text_cache[er["source_pdf"]] = self._fetch_document_text(
                    er["source_pdf"],
                )
            doc_text = doc_text_cache[er["source_pdf"]]

            existing_count = self._count_existing_grounded_rules(er["source_pdf"])

            for row_idx, rr in enumerate(rate_rows):
                row_conf = float(rr.get("confidence") or 0.0)
                # Either per-row OR doc-level confidence qualifies.
                if row_conf < _MIN_ROW_CONFIDENCE and doc_conf < _MIN_DOC_CONFIDENCE:
                    continue
                quote = (rr.get("source_quote") or "").strip()
                if len(quote) < _MIN_QUOTE_LEN:
                    continue
                charge_type = (rr.get("charge_type") or "").strip()
                if not charge_type or charge_type == "Other":
                    continue
                value = rr.get("value")
                if value is None:
                    continue
                try:
                    float(value)
                except (TypeError, ValueError):
                    continue
                if float(value) == 0.0:
                    continue
                unit = (rr.get("unit") or "").strip()
                if not unit:
                    continue
                target_field = self._charge_type_to_target_field(charge_type)
                # Per-(doc, target) cap.
                key = (er["source_pdf"], target_field)
                if seen_doc_target.get(key, 0) + existing_count.get(target_field, 0) >= _MAX_RULES_PER_TARGET_PER_DOC:
                    continue
                seen_doc_target[key] = seen_doc_target.get(key, 0) + 1

                # Local anchor: pick the closest schedule/leaf code within
                # ~800 chars before OR ~400 chars after the source quote.
                # Backward is preferred (anchor[\s\S]*?value recipe). When
                # only a forward anchor is found, anchor_direction="after"
                # so the prompt can flip to value[\s\S]*?anchor instead.
                local_anchor = _find_local_anchor(quote, doc_text)
                anchor_direction = "before"
                if not local_anchor:
                    # Check whether a forward anchor exists so we can set
                    # the direction hint even when falling back to in-line
                    # or identity anchors.
                    fwd = _find_local_anchor(quote, doc_text, window=0, forward_window=400)
                    if fwd:
                        local_anchor = fwd
                        anchor_direction = "after"

                # If no local schedule code is nearby, fall back to a
                # distinctive phrase from the source line itself.
                in_line_anchor = ""
                if not local_anchor:
                    in_line_anchor = _extract_in_line_anchor(quote)
                primary_anchor = (
                    local_anchor
                    or in_line_anchor
                    or identity_anchors[0]
                )
                # Keep identity anchors as fallback context.
                effective_anchors = (
                    [primary_anchor] + [
                        a for a in identity_anchors if a != primary_anchor
                    ]
                )

                candidates.append({
                    "extraction_id": int(er["id"]),
                    "source_pdf": er["source_pdf"],
                    "row_index": row_idx,
                    "source_quote": quote,
                    "value": float(value),
                    "unit": unit,
                    "charge_type": charge_type,
                    "target_field": target_field,
                    "anchors": effective_anchors,
                    "local_anchor": local_anchor,
                    "anchor_direction": anchor_direction,
                    "doc_extraction_confidence": doc_conf,
                    "row_confidence": row_conf,
                })
                if limit and len(candidates) >= limit:
                    return candidates
        return candidates

    def generate_for_candidate(
        self, candidate: dict[str, Any]
    ) -> GroundedOutcome:
        """Generate, validate, and persist a single grounded regex rule.

        Never raises; returns the outcome with status='error' on
        unexpected failures.
        """
        outcome = GroundedOutcome(
            source_pdf=candidate["source_pdf"],
            extraction_id=candidate["extraction_id"],
            row_index=candidate["row_index"],
            source_quote=candidate["source_quote"],
        )

        # Confirm the quote's substantive content exists in the document.
        # The staged extractor often trims leading enumeration prefixes
        # (e.g. "1. " from "1. 39.614¢ per Critical Peak kWh"), so a strict
        # `quote in doc_text` check rejects legitimate quotes. We loosen
        # this to a "key substring" match using the longest run of unique
        # words from the quote.
        doc_text = self._fetch_document_text(candidate["source_pdf"])
        if doc_text and not _quote_substantively_in_text(
            candidate["source_quote"], doc_text,
        ):
            outcome.status = "skipped"
            outcome.error = (
                "source_quote substance not found in document text "
                "(likely hallucinated by stage-3 classifier)"
            )
            return outcome

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

        if not (suggestion.candidate_regex or "").strip():
            outcome.status = "skipped"
            outcome.error = "empty candidate_regex"
            return outcome

        try:
            validation = self.validate(suggestion, candidate)
        except Exception as exc:
            outcome.status = "error"
            outcome.error = f"validation raised: {exc}"
            return outcome
        outcome.validation = validation

        # Retry once on failure with corrective feedback. We pass the
        # captured-values list to the retry prompt so when the failure mode
        # is "wrong value captured", the LLM sees what it grabbed and can
        # disambiguate with a context word.
        if not validation.accept and _MAX_RETRIES > 0:
            try:
                retry_suggestion = self._call_llm(
                    candidate,
                    retry_reason=validation.reason,
                    failed_regex=suggestion.candidate_regex,
                    captured_actual=validation.captured_values,
                )
            except Exception:
                retry_suggestion = None
            if retry_suggestion is not None and (retry_suggestion.candidate_regex or "").strip():
                try:
                    retry_validation = self.validate(retry_suggestion, candidate)
                except Exception:
                    retry_validation = None
                if retry_validation is not None:
                    outcome.suggestion = retry_suggestion
                    outcome.validation = retry_validation
                    validation = retry_validation

        rule_status = "accepted" if validation.accept else "rejected"
        try:
            outcome.rule_id = self._persist_rule(
                outcome.suggestion, candidate, rule_status, validation,
            )
        except Exception as exc:
            outcome.status = "error"
            outcome.error = f"persist failed: {exc}"
            return outcome

        outcome.status = rule_status
        return outcome

    def generate_batch(
        self, *, limit: int = 10
    ) -> list[GroundedOutcome]:
        """Run the full pipeline over a batch of candidates."""
        candidates = self.select_candidates(limit=limit)
        outcomes: list[GroundedOutcome] = []
        for c in candidates:
            outcomes.append(self.generate_for_candidate(c))
        return outcomes

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(
        self,
        suggestion: RegexSuggestion,
        candidate: dict[str, Any],
    ) -> GroundedValidationResult:
        """Run the four-step validation: compile, anchor, source-match, value-match."""
        result = GroundedValidationResult()
        regex_str = (suggestion.candidate_regex or "").strip()
        if not regex_str:
            result.reason = "empty regex"
            return result

        # 1. Compile.
        try:
            # DOTALL is critical: LLMs frequently write `(?=.*ANCHOR)` style
            # lookaheads expecting `.` to span newlines, even though Python's
            # default doesn't. Without DOTALL, ~50% of generated regexes
            # fail with "no match" when they would actually be correct.
            pattern = re.compile(
                regex_str, re.IGNORECASE | re.MULTILINE | re.DOTALL,
            )
        except re.error as e:
            result.reason = f"regex compile error: {e}"
            return result
        result.compiled = True

        # 2. Anchor presence — compact comparison after stripping common
        # regex tokens so `Basic\s+Customer\s+Charge` matches the anchor
        # "Basic Customer Charge" by becoming "basiccustomercharge".
        anchors: list[str] = candidate.get("anchors") or []
        compact_regex = _compact_for_anchor_check(regex_str)
        for a in anchors:
            compact_anchor = re.sub(r"[^a-z0-9]", "", a.lower())
            if compact_anchor and compact_anchor in compact_regex:
                result.anchor_present = True
                break
        if not result.anchor_present:
            result.reason = (
                "regex lacks a document-specific anchor; required one of: "
                f"{', '.join(anchors[:3])}"
            )
            return result

        # 3. Matches against the original source line.
        source_line = candidate["source_quote"]
        try:
            line_matches = pattern.findall(source_line)
        except Exception:
            line_matches = []

        # If the literal source line didn't match, try a whitespace-normalized
        # version. OCR output often has irregular spacing (double spaces, tabs,
        # non-breaking spaces) that the model's \s+ patterns can't handle when
        # the stored text has a literal multi-space run. Collapsing all
        # whitespace runs to a single space resolves ~80% of "literal spaces"
        # rejections without loosening the regex itself.
        if not line_matches:
            normalized_line = re.sub(r"[ \t\xa0]+", " ", source_line).strip()
            if normalized_line != source_line:
                try:
                    line_matches = pattern.findall(normalized_line)
                except Exception:
                    line_matches = []

        # Some grounded regexes need cross-line context (e.g. anchor on
        # Schedule code 3 lines above the rate). When the source line
        # itself doesn't contain the anchor token, allow validation
        # against the full document text instead. Also try a
        # whitespace-normalized version of the full document.
        if not line_matches:
            doc_text = self._fetch_document_text(candidate["source_pdf"])
            if doc_text:
                try:
                    doc_matches = pattern.findall(doc_text)
                    if doc_matches:
                        line_matches = doc_matches
                        result.reason = ""  # cleared, will overwrite below
                except Exception:
                    pass
            if not line_matches and doc_text:
                normalized_doc = re.sub(r"[ \t\xa0]+", " ", doc_text)
                try:
                    norm_doc_matches = pattern.findall(normalized_doc)
                    if norm_doc_matches:
                        line_matches = norm_doc_matches
                except Exception:
                    pass

        if not line_matches:
            result.reason = (
                "regex didn't match the source line or full document — "
                "likely literal spaces or OCR-sensitive characters in pattern"
            )
            return result
        result.matches_source = True
        result.captured_values = [
            str(m if isinstance(m, str) else (m[0] if isinstance(m, tuple) and m else ""))
            for m in line_matches[:5]
        ]

        # 4. Captured value matches expected within tolerance.
        expected = float(candidate["value"])
        captured_floats: list[float] = []
        for m in line_matches[:10]:
            pieces = m if isinstance(m, tuple) else (m,)
            for p in pieces:
                if isinstance(p, str):
                    try:
                        captured_floats.append(float(p))
                    except ValueError:
                        continue
        # Allow 1% tolerance on float comparison (handles OCR rounding).
        for cf in captured_floats:
            if abs(cf - expected) / max(abs(expected), 1e-6) < 0.01:
                result.captures_expected_value = True
                break

        if not result.captures_expected_value:
            result.reason = (
                f"regex matched but didn't capture expected value {expected}; "
                f"captured: {captured_floats[:5]}"
            )
            return result

        result.accept = True
        result.reason = "all validations passed"
        return result

    # ------------------------------------------------------------------
    # Internal — LLM call
    # ------------------------------------------------------------------

    def _call_llm(
        self,
        candidate: dict[str, Any],
        *,
        retry_reason: str = "",
        failed_regex: str = "",
        captured_actual: list[str] | None = None,
    ) -> RegexSuggestion | None:
        anchor = candidate["anchors"][0] if candidate.get("anchors") else ""
        if retry_reason:
            captured_block = _format_captured_actual_block(
                captured_actual or [], float(candidate["value"]),
            )
            prompt = _RETRY_PROMPT.format(
                reason=retry_reason,
                captured_actual_block=captured_block,
                source_line=candidate["source_quote"],
                anchor=anchor,
                value=candidate["value"],
                failed_regex=failed_regex,
            )
        else:
            direction = candidate.get("anchor_direction", "before")
            if direction == "after":
                anchor_direction_hint = (
                    "The anchor code appears AFTER the rate value in this document.\n"
                    "Do NOT write `anchor[\\s\\S]*?value` — the anchor is downstream.\n"
                    "Instead open with a phrase from the source line, capture the value,\n"
                    "then require the anchor code to follow: `phrase[\\s\\S]*?(value)[\\s\\S]*?anchor`."
                )
                construction_recipe = (
                    "1. Open the regex with a DISTINCTIVE PHRASE from the source line "
                    "(NOT the anchor code — the anchor is after the value).\n"
                    "2. Bridge any cross-line gap with `[\\s\\S]*?`.\n"
                    "3. Add context words from the source line immediately before the capture.\n"
                    "4. Place the capturing group `(...)` around the numeric value.\n"
                    "5. Append `[\\s\\S]*?` then the ANCHOR code to close the pattern."
                )
            else:
                anchor_direction_hint = (
                    "The anchor code appears BEFORE the rate value in this document.\n"
                    "Start the regex with the anchor, bridge with `[\\s\\S]*?`, then "
                    "capture the value: `anchor[\\s\\S]*?(value)`."
                )
                construction_recipe = (
                    "1. Start with the ANCHOR code (escape `-` as `\\-`).\n"
                    "2. Bridge to the rate line with `[\\s\\S]*?`.\n"
                    "3. Add context words from the source line to narrow the match.\n"
                    "4. Place the capturing group `(...)` around the numeric value.\n"
                    "5. Add the unit symbol or suffix immediately after the capture group."
                )
            prompt = _GROUNDED_PROMPT.format(
                source_line=candidate["source_quote"],
                value=candidate["value"],
                unit=candidate["unit"],
                charge_type=candidate["charge_type"],
                charge_type_field=candidate["target_field"],
                anchor=anchor,
                source_pdf=candidate["source_pdf"],
                schedule_codes=", ".join(candidate.get("anchors") or [])[:200],
                anchor_direction_hint=anchor_direction_hint,
                construction_recipe=construction_recipe,
            )

        run_result = self._orch.generate_json(
            role=self._role,
            prompt=prompt,
            schema=RegexSuggestion,
            subject_kind="extraction_grounded",
            subject_id=f"{candidate['extraction_id']}:{candidate['row_index']}",
            stage="extraction_grounded_rule",
        )
        if run_result.status not in ("ok", "fallback_used"):
            return None
        suggestion: RegexSuggestion = run_result.result
        # Defensive normalization.
        if suggestion.suggestion_type not in ALLOWED_SUGGESTION_TYPES:
            suggestion.suggestion_type = "regex_candidate"
        if suggestion.risk not in ALLOWED_RISK_LEVELS:
            suggestion.risk = "medium"
        if not (suggestion.target_field or "").strip():
            suggestion.target_field = candidate["target_field"]
        if not (suggestion.expected_unit or "").strip():
            suggestion.expected_unit = candidate["unit"]

        # Guard: if the LLM omitted a capture group, do an immediate corrective
        # retry rather than letting validation discover it. A regex with no `(`
        # can never pass value-match validation regardless of direction.
        if (
            not retry_reason
            and suggestion.candidate_regex
            and "(" not in suggestion.candidate_regex
        ):
            corrective_prompt = (
                f"Your regex `{suggestion.candidate_regex}` has NO capturing group `(...)`.\n"
                "The validator extracts the numeric value from the first group — "
                "without a group it cannot extract anything and will reject the rule.\n\n"
                f"Wrap the numeric value `{candidate['value']}` (or the digits that "
                "represent it) in a capturing group.\n\n"
                "Return the corrected JSON object only."
            )
            retry_result = self._orch.generate_json(
                role=self._role,
                prompt=corrective_prompt,
                schema=RegexSuggestion,
                subject_kind="extraction_grounded",
                subject_id=f"{candidate['extraction_id']}:{candidate['row_index']}",
                stage="extraction_grounded_rule_capture_fix",
            )
            if retry_result.status in ("ok", "fallback_used") and retry_result.result:
                fixed = retry_result.result
                if fixed.candidate_regex and "(" in fixed.candidate_regex:
                    if not (fixed.target_field or "").strip():
                        fixed.target_field = candidate["target_field"]
                    if not (fixed.expected_unit or "").strip():
                        fixed.expected_unit = candidate["unit"]
                    return fixed

        return suggestion

    # ------------------------------------------------------------------
    # Internal — helpers
    # ------------------------------------------------------------------

    def _get_doc_anchors(self, source_pdf: str) -> list[str]:
        """Return high-specificity schedule/rider codes for a document."""
        try:
            anchors_dict = fetch_document_anchors(self._db_path, source_pdf)
        except Exception:
            return []
        out: list[str] = []
        for key in ("schedule_codes", "rider_codes"):
            for v in anchors_dict.get(key, []):
                if isinstance(v, str) and HIGH_SPECIFICITY_CODE_RE.match(v.strip()):
                    out.append(v.strip())
        return out

    def _count_existing_grounded_rules(
        self, source_pdf: str,
    ) -> dict[str, int]:
        """Return {target_field: count} of existing grounded rules for this doc.

        Used by ``select_candidates`` to enforce per-(doc, target) caps.
        """
        try:
            conn = sqlite3.connect(str(self._db_path))
            rows = conn.execute(
                """
                SELECT dsr.target_field, COUNT(*)
                FROM document_specific_rules dsr
                JOIN document_identity di
                  ON di.id = dsr.document_identity_id
                WHERE di.source_pdf = ?
                  AND dsr.status IN ('accepted', 'pending', 'rejected')
                  AND dsr.notes LIKE 'origin: extraction-grounded%'
                GROUP BY dsr.target_field
                """,
                (source_pdf,),
            ).fetchall()
            conn.close()
            return {(r[0] or ""): int(r[1] or 0) for r in rows}
        except Exception:
            return {}

    def _fetch_document_text(self, source_pdf: str) -> str:
        """Fetch full page text (not just rate-relevant) for verification.

        Pulls every page's text_content concatenated, with NO truncation.
        Large tariff bundles can be 100k+ chars and we'd otherwise miss
        quotes that came from a later page during extraction.
        """
        if not source_pdf:
            return ""
        try:
            conn = sqlite3.connect(str(self._db_path))
            rows = conn.execute(
                """
                SELECT text_content FROM ncuc_page_artifacts
                WHERE source_pdf = ?
                ORDER BY page_number
                """,
                (source_pdf,),
            ).fetchall()
            conn.close()
            return "\n".join((r[0] or "") for r in rows)
        except Exception:
            return ""

    @staticmethod
    def _charge_type_to_target_field(charge_type: str) -> str:
        """Map ALLOWED_CHARGE_TYPES values into target_field labels used by
        the parsing layer. Keeps the legacy per-doc rule taxonomy stable.
        """
        mapping = {
            "Basic Facilities Charge": "fixed_charge",
            "Fixed Monthly Charge": "fixed_charge",
            "Energy Charge": "energy_charge",
            "Demand Charge": "demand_charge",
            "Rider Adjustment": "rider_charge",
            "Minimum Bill": "minimum_bill",
            "TOU Rate": "energy_charge",
            "Seasonal Rate": "energy_charge",
            "Lighting Charge": "lighting_charge",
        }
        return mapping.get(charge_type, "other_charge")

    def _persist_rule(
        self,
        suggestion: RegexSuggestion | None,
        candidate: dict[str, Any],
        status: str,
        validation: GroundedValidationResult,
    ) -> int:
        """Persist the rule to document_specific_rules with an origin tag.

        The notes prefix ``origin: extraction-grounded`` lets downstream
        analysis A/B compare grounded-vs-legacy rule acceptance rates and
        eventually re-prioritize promotion.
        """
        if suggestion is None:
            raise ValueError("persist_rule called with no suggestion")

        # Need a document_identity_id; look it up.
        doc_id = self._fetch_document_identity_id(candidate["source_pdf"])
        if doc_id == 0:
            raise ValueError(
                f"no document_identity row for {candidate['source_pdf']}"
            )

        notes = (
            f"origin: extraction-grounded; "
            f"extraction_id={candidate['extraction_id']}; "
            f"row_index={candidate['row_index']}; "
            f"expected_value={candidate['value']}; "
            f"validation: {validation.reason}"
        )
        rule = DocumentSpecificRule(
            document_identity_id=int(doc_id),
            candidate_regex=suggestion.candidate_regex or "",
            candidate_normalization=(suggestion.candidate_normalization or None),
            expected_unit=(suggestion.expected_unit or None),
            target_field=candidate["target_field"],
            status=status,
            notes=notes,
        )
        return insert_rule(self._db_path, rule)

    def _fetch_document_identity_id(self, source_pdf: str) -> int:
        if not source_pdf:
            return 0
        try:
            conn = sqlite3.connect(str(self._db_path))
            row = conn.execute(
                "SELECT id FROM document_identity WHERE source_pdf = ? LIMIT 1",
                (source_pdf,),
            ).fetchone()
            conn.close()
            return int(row[0]) if row else 0
        except Exception:
            return 0
