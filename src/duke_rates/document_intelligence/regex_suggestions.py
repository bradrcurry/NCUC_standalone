"""
LLM-assisted regex / normalization suggestion generation (Phase 5.6).

For parse failures classified as ``regex_gap``, ``normalization_gap``, or
``ocr_noise``, asks an LLM to suggest candidate fixes.

Suggestions are ADVISORY only — they are persisted for review and must pass
a deterministic validation harness before being accepted. The LLM never
directly edits parser code.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from duke_rates.document_intelligence.ollama_orchestrator import OllamaOrchestrator

# High-specificity schedule/rider code pattern. Used to filter the noisy
# fingerprinter output (which captures English words like "IS", "MAY")
# down to real codes like RES-28, MGS-32, SGS76, LGS-3.
HIGH_SPECIFICITY_CODE_RE = re.compile(r"^[A-Z]{2,5}-?\d{1,3}[A-Z]?$")

# Title fragments that show up across many profiles and shouldn't be used
# as anchors. Mirrors profile_consensus.TITLE_BLOCKLIST.
ANCHOR_TITLE_BLOCKLIST = {
    "AVAILABILITY", "CERTIFICATE OF SERVICE", "(NORTH CAROLINA ONLY)",
    "ASSOCIATE GENERAL COUNSEL", "DEPUTY GENERAL COUNSEL", "F I L ED",
    "FILED", "DEFINITIONS", "MAILING ADDRESS:", "APPLICABILITY",
    "ENERGY.", "DUKE ENERGY", "DUKE ENERGY CAROLINAS",
}

# ---------------------------------------------------------------------------
# Allowed enumerations
# ---------------------------------------------------------------------------

ALLOWED_SUGGESTION_TYPES: tuple[str, ...] = (
    "normalization_rule",
    "regex_candidate",
    "parser_profile_hint",
    "section_boundary_rule",
)

ALLOWED_RISK_LEVELS: tuple[str, ...] = (
    "low",
    "medium",
    "high",
)

ALLOWED_SUGGESTION_STATUSES: tuple[str, ...] = (
    "pending_review",
    "accepted_candidate",
    "rejected_false_positive",
    "rejected_no_gain",
    "needs_human_review",
)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class TestCase(BaseModel):
    text: str = Field(description="A line or excerpt of text")
    should_match: bool = Field(description="Whether the candidate regex should match this text")


class RegexSuggestion(BaseModel):
    """Strict JSON output the LLM must produce for a regex / normalization fix."""

    suggestion_type: str = Field(
        default="regex_candidate",
        description=f"One of: {', '.join(ALLOWED_SUGGESTION_TYPES)}",
    )
    target_profile: str = Field(default="", description="Parser profile this targets")
    target_field: str = Field(default="", description="Field name this would help extract")
    missed_text: str = Field(default="", description="The text the current parser missed")
    likely_issue: str = Field(
        default="", description="Short description of why the current parser fails here"
    )
    candidate_regex: str = Field(
        default="", description="Proposed regex pattern (empty for normalization-only suggestions)"
    )
    candidate_normalization: str = Field(
        default="",
        description="Proposed normalization step (empty for regex-only suggestions)",
    )
    expected_unit: str = Field(default="", description="Expected unit if known (e.g. kWh, $/month)")
    risk: str = Field(default="medium", description=f"One of: {', '.join(ALLOWED_RISK_LEVELS)}")
    positive_test_cases: list[TestCase] = Field(default_factory=list)
    negative_test_cases: list[TestCase] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="before")
    @classmethod
    def normalize_model_output(cls, data: Any) -> Any:
        """Accept common local-LLM JSON variants for regex suggestions."""
        if not isinstance(data, dict):
            return data

        if not data.get("risk") and isinstance(data.get("risk_level"), str):
            data["risk"] = data["risk_level"]

        if not data.get("target_field"):
            for key in ("expected_field", "field", "field_name"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    data["target_field"] = value.strip()
                    break

        if not data.get("likely_issue"):
            for key in ("description", "rationale", "reasoning", "issue"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    data["likely_issue"] = value.strip()
                    break

        data["positive_test_cases"] = _normalize_test_cases(
            data.get("positive_test_cases"),
            should_match=True,
        )
        data["negative_test_cases"] = _normalize_test_cases(
            data.get("negative_test_cases"),
            should_match=False,
        )
        return data


def _normalize_test_cases(value: Any, *, should_match: bool) -> list[Any]:
    """Normalize model-emitted test cases into TestCase-shaped objects."""
    if not isinstance(value, list):
        return []

    normalized: list[Any] = []
    for item in value:
        if isinstance(item, str):
            if item.strip():
                normalized.append({"text": item.strip(), "should_match": should_match})
            continue
        if isinstance(item, dict):
            row = dict(item)
            if "text" not in row:
                for key in ("value", "example", "case", "input"):
                    if isinstance(row.get(key), str):
                        row["text"] = row[key]
                        break
            row.setdefault("should_match", should_match)
            normalized.append(row)
    return normalized


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

_SUGGESTION_SYSTEM_PROMPT = """\
You are a tariff document parsing expert for the North Carolina Utilities Commission (NCUC).
Your task is to suggest a regex pattern or text normalization rule that would help a
deterministic parser extract charges from tariff documents that it currently fails on.

## Failure context:
- Failure type: {failure_type}
- Failed field (if known): {target_field}
- Parser profile: {target_profile}
- Current regex pattern names in profile: {current_patterns}
- Expected output schema: {expected_schema}

## DOCUMENT-SPECIFIC ANCHORS (REQUIRED):
The candidate regex MUST include at least one of these anchors so it does NOT
match unrelated documents in the corpus. A regex without an anchor will be
rejected automatically as too generic.

{document_anchors}

## RATE CANDIDATES (pre-scan results):
A pre-scan of the document found these likely rate values. Your regex should
capture one or more of these — they confirm rates exist and show their format:
{rate_candidates}

## PYTHON REGEX CONSTRAINTS (critical — violations cause automatic rejection):
Python's `re` module has these limitations you MUST respect:
- NO variable-length look-behinds. `(?<=X.*)Y` is INVALID. Use capturing
  groups `(...)` instead of look-behinds. Fixed-length look-behinds like
  `(?<=Schedule RES-28)` are OK.
- `.` does NOT match newlines. Use `[\\s\\S]*?` instead of `.*?` when you
  need to cross lines (e.g. `Schedule RES-28[\\s\\S]*?\\$(\\d+\\.\\d+)`).
- For named groups use `(?P<name>...)` NOT the PCRE `(?<name>...)` syntax.
- `\\d` matches [0-9], `\\s` matches whitespace, `\\w` matches [a-zA-Z0-9_].
- OCR text has INCONSISTENT whitespace — always use `\\s+` (1+ spaces) or
  `\\s*` (0+ spaces) between tokens rather than literal spaces. For example
  use `Payment:\\s+\\$` NOT `Payment: \\$` because OCR may have double spaces.

## Text the current parser MISSED (rate-relevant pages only):
```
{missed_text}
```

## Examples of SUCCESSFUL matches from the same profile:
{successful_examples}

## Example of a working regex (for reference):
Profile: progress_residential_tou
Document anchor: RES-28
Working regex: Schedule\\s+RES-28[\\s\\S]*?Basic\\s+Facilities\\s+Charge[\\s\\S]*?\\$(\\d+\\.\\d+)
This works because: (1) it anchors to a specific schedule code, (2) uses
[\\s\\S]*? to cross lines between the schedule header and the charge, (3)
captures the dollar amount with a simple numeric group.

## Instructions:
1. suggestion_type MUST be one of: {allowed_types}
2. risk MUST be one of: {allowed_risks}
3. If the issue is text normalization (OCR artifacts, symbol noise), provide
   candidate_normalization instead of a regex.
4. candidate_regex MUST contain at least one document-specific anchor from above.
   Include the code literally: `Schedule\\s+RES-28` or `Rider\\s+EB` or
   `LEAF\\s+NO\\.\\s*331`. Generic patterns like `\\$\\d+\\.\\d+\\s*/kWh`
   without an anchor will be rejected.
5. NCUC tariff pages often express energy rates in cents (e.g. `10.369¢ per kWh`).
   In that case capture the numeric cents value, set expected_unit to `¢/kWh`,
   and set candidate_normalization to `divide captured cents per kWh by 100`.
6. Provide 2-5 positive_test_cases (exact lines from the MISSED TEXT that SHOULD
   match) and 2-5 negative_test_cases (lines that should NOT match — include at
   least one from a DIFFERENT schedule code).
7. expected_unit should be $/kWh, ¢/kWh, $/month, $/kW, etc. or empty if unknown.
8. confidence 0.0-1.0: how specific, evidence-backed, and testable the suggestion is.
9. Return an EMPTY candidate_regex if no plausible pattern exists — do not guess.

Respond with a single JSON object matching the required schema. No other text."""


_RETRY_SUGGESTION_PROMPT = """\
Your previous regex was REJECTED. Fix the issues below and return a corrected regex.

## Why your regex failed: {failure_reason}

## Document anchors you MUST include:
{document_anchors}

## MISSED TEXT (same document):
```
{missed_text}
```

## Your failed candidate_regex:
```
{failed_regex}
```

## Instructions:
- suggestion_type MUST be one of: {allowed_types}
- risk MUST be one of: {allowed_risks}
- Fix the specific issue described above. If the regex didn't match, make it
  less rigid — allow for OCR noise, variable whitespace, and line breaks.
  Use `[\\s\\S]*?` to cross lines instead of literal newlines.
- If the regex lacks a document anchor, add one from the anchor list above.
- If the regex failed to compile, check for Python re violations: no
  variable-length look-behinds, no PCRE named groups `(?<name>...)`.

Return a corrected JSON object with a fixed candidate_regex. No other text."""


# ---------------------------------------------------------------------------
# Document-anchor extraction (Phase 0B)
# ---------------------------------------------------------------------------


def fetch_document_anchors(
    db_path: Path | str, source_pdf: str, *, max_titles: int = 4
) -> dict[str, list[str]]:
    """Pull high-specificity anchors for one document from the fingerprinter.

    Returns a dict with keys ``schedule_codes``, ``rider_codes``,
    ``leaf_numbers``, ``titles``. Only high-specificity items are returned
    (real schedule codes, distinctive titles) — generic English words and
    boilerplate are filtered out.

    The result is suitable both for prompt injection (via
    :func:`render_anchors_for_prompt`) and for post-validation checks (via
    :func:`regex_contains_anchor`).
    """
    anchors: dict[str, list[str]] = {
        "schedule_codes": [],
        "rider_codes": [],
        "leaf_numbers": [],
        "titles": [],
    }
    if not source_pdf:
        return anchors

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            """
            SELECT schedule_codes_json, rider_codes_json,
                   leaf_numbers_json, title_candidates_json
            FROM document_fingerprints_v2
            WHERE source_pdf = ?
            LIMIT 1
            """,
            (source_pdf,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return anchors

    def _load(j: Any) -> list[str]:
        try:
            data = json.loads(j or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
        return [s for s in data if isinstance(s, str)]

    for c in _load(row[0]):
        if HIGH_SPECIFICITY_CODE_RE.match(c):
            anchors["schedule_codes"].append(c)
    for c in _load(row[1]):
        if HIGH_SPECIFICITY_CODE_RE.match(c):
            anchors["rider_codes"].append(c)
    for c in _load(row[2]):
        if c.strip():
            anchors["leaf_numbers"].append(c.strip())
    for t in _load(row[3])[:max_titles * 4]:
        t_norm = t.upper().strip()
        if not t_norm or t_norm in ANCHOR_TITLE_BLOCKLIST:
            continue
        if 8 <= len(t_norm) <= 80:
            anchors["titles"].append(t_norm)
            if len(anchors["titles"]) >= max_titles:
                break

    # Deduplicate while preserving order
    for key, vals in anchors.items():
        seen: set[str] = set()
        deduped: list[str] = []
        for v in vals:
            if v not in seen:
                seen.add(v)
                deduped.append(v)
        anchors[key] = deduped
    return anchors


def render_anchors_for_prompt(anchors: dict[str, list[str]]) -> str:
    """Render an anchor dict as a bulleted prompt block.

    Returns ``"(no document-specific anchors available — fall back to the
    profile name; expect this regex to be stricter to compensate)"`` when
    the doc has no usable anchors.
    """
    parts: list[str] = []
    if anchors.get("schedule_codes"):
        parts.append("- Schedule codes: " + ", ".join(anchors["schedule_codes"]))
    if anchors.get("rider_codes"):
        parts.append("- Rider codes: " + ", ".join(anchors["rider_codes"]))
    if anchors.get("leaf_numbers"):
        parts.append("- Leaf numbers: " + ", ".join(anchors["leaf_numbers"]))
    if anchors.get("titles"):
        parts.append("- Distinctive title fragments:")
        for t in anchors["titles"]:
            parts.append(f"    * {t!r}")
    if not parts:
        return (
            "(no document-specific anchors available — fall back to the\n"
            "profile name; expect this regex to be stricter to compensate)"
        )
    return "\n".join(parts)


def regex_contains_anchor(regex_pattern: str, anchors: dict[str, list[str]]) -> bool:
    """Return True if the regex string contains any anchor from the bundle.

    Used by the validation harness to reject low-specificity regexes before
    running the corpus check.
    """
    if not regex_pattern:
        return False
    pattern_upper = regex_pattern.upper()
    for key in ("schedule_codes", "rider_codes", "leaf_numbers"):
        for code in anchors.get(key, []):
            # Codes may appear as RES-28 or RES\-28 in regex; check both.
            if code in regex_pattern or code.replace("-", r"\-") in regex_pattern:
                return True
    for title in anchors.get("titles", []):
        # Titles are case-insensitive; check 12+ char substring presence.
        for window in (title[:20], title[-20:], title):
            window = window.strip()
            if len(window) >= 8 and window in pattern_upper:
                return True
    return False


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


def _regex_context_excerpt(text: str, limit: int = 1500) -> str:
    """Keep regex-relevant rate lines visible in long document excerpts."""
    cleaned = str(text or "").strip()
    if len(cleaned) <= limit:
        return cleaned

    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    scored: list[tuple[int, int]] = []
    for idx, line in enumerate(lines):
        lower = line.lower()
        score = 0
        if any(marker in line for marker in ("$", "¢", "%")):
            score += 2
        if any(term in lower for term in ("kwh", "kw", "therm", "per month", "/month", "/kwh", "/kw")):
            score += 4
        if any(term in lower for term in ("rate", "charge", "payment", "incentive", "savings", "credit")):
            score += 2
        if any(ch.isdigit() for ch in line):
            score += 1
        if score >= 5:
            scored.append((score, idx))

    if not scored:
        return cleaned[:limit]

    selected: set[int] = set()
    for _, idx in sorted(scored, reverse=True)[:8]:
        selected.update(range(max(0, idx - 1), min(len(lines), idx + 2)))

    excerpt_lines: list[str] = []
    last_idx = -2
    for idx in sorted(selected):
        if idx != last_idx + 1 and excerpt_lines:
            excerpt_lines.append("...")
        excerpt_lines.append(lines[idx])
        last_idx = idx

    excerpt = "\n".join(excerpt_lines)
    if len(excerpt) <= limit:
        return excerpt
    return excerpt[:limit]


class RegexSuggestionGenerator:
    """LLM-assisted regex / normalization suggestion generation.

    Parameters
    ----------
    orchestrator : OllamaOrchestrator
        Phase 2.5 orchestrator. Must have ``regex_suggestion`` role.
    db_path : Path
        Path to the SQLite database.
    role : str
        Orchestrator role (default ``"regex_suggestion"``).
    """

    def __init__(
        self,
        orchestrator: OllamaOrchestrator,
        db_path: Path,
        *,
        role: str = "regex_suggestion",
    ) -> None:
        self._orch = orchestrator
        self._db_path = db_path
        self._role = role

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select_diagnoses_for_suggestion(
        self,
        *,
        limit: int = 10,
        diagnosis_id: int | None = None,
        profile: str | None = None,
        failure_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query ``llm_parse_diagnostics`` for eligible failure types.

        Only returns diagnoses with failure_type in (regex_gap, normalization_gap,
        ocr_noise) that haven't already had suggestions generated.
        """
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            params: list[Any] = []
            extra_where = ""

            if diagnosis_id:
                extra_where += " AND ld.id = ?"
                params.append(diagnosis_id)
            if profile:
                extra_where += " AND ld.parse_attempt_id IN (SELECT id FROM parse_attempt_logs WHERE parser_profile = ?)"
                params.append(profile)
            if failure_type:
                extra_where += " AND ld.failure_type = ?"
                params.append(failure_type)

            rows = conn.execute(
                f"""
                SELECT ld.id AS diagnosis_id,
                       ld.parse_attempt_id,
                       ld.failure_type,
                       ld.confidence AS diagnosis_confidence,
                       ld.evidence_json,
                       ld.recommended_action,
                       pal.source_pdf,
                       pal.parser_profile,
                       pal.metadata_json,
                       pal.effective_date
                FROM llm_parse_diagnostics ld
                LEFT JOIN parse_attempt_logs pal ON pal.id = ld.parse_attempt_id
                WHERE ld.failure_type IN ('regex_gap', 'normalization_gap', 'ocr_noise')
                  AND ld.id NOT IN (
                      SELECT diagnosis_id FROM llm_regex_suggestions
                      WHERE diagnosis_id IS NOT NULL
                  )
                  {extra_where}
                ORDER BY ld.confidence DESC
                LIMIT ?
                """,
                tuple(params + [limit]),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_current_patterns(self, parser_profile: str) -> str:
        """Get regex pattern names used by a profile."""
        # Query successful parse attempts from this profile for metadata
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT metadata_json
                FROM parse_attempt_logs
                WHERE parser_profile = ?
                  AND status = 'parsed'
                  AND charge_count > 0
                ORDER BY id DESC
                LIMIT 1
                """,
                (parser_profile,),
            ).fetchone()
            if row and row["metadata_json"]:
                try:
                    meta = json.loads(row["metadata_json"])
                    patterns = meta.get("patterns_used", [])
                    if patterns:
                        return ", ".join(str(p) for p in patterns[:20])
                except (json.JSONDecodeError, TypeError):
                    pass
        except Exception:
            pass
        finally:
            conn.close()
        return f"Profile: {parser_profile} — specific pattern names not recoverable"

    def get_successful_examples(self, parser_profile: str) -> str:
        """Get examples of successful parse output from the same profile."""
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT source_pdf, charge_count, metadata_json
                FROM parse_attempt_logs
                WHERE parser_profile = ?
                  AND status = 'parsed'
                  AND charge_count > 0
                ORDER BY id DESC
                LIMIT 3
                """,
                (parser_profile,),
            ).fetchall()
            if rows:
                parts: list[str] = []
                for r in rows:
                    parts.append(
                        f"  - {r['source_pdf']}: {r['charge_count']} charges extracted"
                    )
                return "\n".join(parts)
        except Exception:
            pass
        finally:
            conn.close()
        return "(no successful examples found for this profile)"

    def get_expected_schema(self, parser_profile: str) -> str:
        """Describe the expected charge fields for a profile."""
        # Map known profiles to their expected fields
        profile_schemas: dict[str, str] = {
            "generic_residential": "Basic Facilities Charge ($/month), Energy Charge (kWh, possibly seasonal/TOU), Demand Charge if applicable (kW)",
            "progress_residential_tou": "Basic Facilities Charge, On-Peak Energy (kWh), Off-Peak Energy (kWh), possibly seasonal tiers",
            "progress_billing_adjustments": "Rider adjustment charges with rider code, rate per kWh or fixed amount",
            "progress_single_value_rider": "Single rider adjustment per kWh or per month",
            "progress_rider_adjustment_matrix": "Multiple rider adjustments — one per rider code, with rate and unit",
            "carolinas_rider_adjustment_matrix": "Multiple rider adjustments for Carolinas — one per rider code",
            "carolinas_residential": "Basic Facilities Charge, Energy Charge (kWh), possibly seasonal",
            "carolinas_lighting_schedule": "Lighting charges — per fixture or per kWh, possibly by lamp type/wattage",
        }
        for key, desc in profile_schemas.items():
            if key in parser_profile:
                return desc
        return "Rate charges: typically a charge description, value, unit ($/kWh, $/month, $/kW, kWh), optional season or TOU period"

    def generate_suggestion(
        self, diagnosis_row: dict[str, Any], *, retry_on_failure: bool = True
    ) -> RegexSuggestion | None:
        """Generate a regex/normalization suggestion for one diagnosis. Never raises.

        When *retry_on_failure* is True (the default), the generated regex is
        compiled and tested against the document text. If it fails (compile
        error, zero matches, missing anchor), the LLM is re-prompted once with
        specific feedback about what went wrong.
        """
        diagnosis_id = diagnosis_row.get("diagnosis_id", 0)
        parse_attempt_id = diagnosis_row.get("parse_attempt_id", 0)
        failure_type = diagnosis_row.get("failure_type", "unknown")
        parser_profile = diagnosis_row.get("parser_profile") or "unknown"

        # Gather context
        parse_meta: dict[str, Any] = {}
        try:
            raw_meta = diagnosis_row.get("metadata_json", "{}")
            parse_meta = json.loads(raw_meta) if raw_meta else {}
        except (json.JSONDecodeError, TypeError):
            pass

        # Get text from parse_attempt_logs metadata or page artifacts
        missed_text = self._get_missed_text(diagnosis_row, parse_meta)
        current_patterns = self.get_current_patterns(parser_profile)
        successful_examples = self.get_successful_examples(parser_profile)
        expected_schema = self.get_expected_schema(parser_profile)
        target_field = parse_meta.get("target_field", "") or ""
        # Phase 0B: pull document-specific anchors from the fingerprinter so
        # the LLM is required to scope its regex to this doc, not the whole
        # profile. Without this, ~72% of suggestions get auto-rejected as
        # broad false positives.
        source_pdf = diagnosis_row.get("source_pdf") or ""
        anchors = fetch_document_anchors(self._db_path, source_pdf)
        document_anchors = render_anchors_for_prompt(anchors)
        rate_candidates = self._scan_rate_candidates(missed_text)

        prompt = _SUGGESTION_SYSTEM_PROMPT.format(
            failure_type=failure_type,
            target_field=target_field or "(unknown)",
            target_profile=parser_profile,
            current_patterns=current_patterns,
            expected_schema=expected_schema,
            document_anchors=document_anchors,
            rate_candidates=rate_candidates,
            missed_text=missed_text[:1500] if missed_text else "(no text available)",
            successful_examples=successful_examples,
            allowed_types=", ".join(ALLOWED_SUGGESTION_TYPES),
            allowed_risks=", ".join(ALLOWED_RISK_LEVELS),
        )

        try:
            run_result = self._orch.generate_json(
                role=self._role,
                prompt=prompt,
                schema=RegexSuggestion,
                subject_kind="parse_attempt",
                subject_id=str(parse_attempt_id),
                stage="regex_suggestion",
            )
        except Exception:
            return None

        if run_result.status not in ("ok", "fallback_used"):
            return None

        suggestion: RegexSuggestion = run_result.result

        # Validate enumerations
        if suggestion.suggestion_type not in ALLOWED_SUGGESTION_TYPES:
            suggestion.suggestion_type = "regex_candidate"
        if suggestion.risk not in ALLOWED_RISK_LEVELS:
            suggestion.risk = "medium"

        # Backfill target_profile from the diagnosis when the LLM left it blank.
        if not (suggestion.target_profile or "").strip():
            suggestion.target_profile = parser_profile

        # --- Iterative refinement: test regex, retry on failure ---
        if retry_on_failure and (suggestion.candidate_regex or "").strip():
            failure_reason = self._test_candidate_regex(
                suggestion.candidate_regex or "",
                missed_text,
                anchors,
            )
            if failure_reason:
                retry_prompt = _RETRY_SUGGESTION_PROMPT.format(
                    failure_reason=failure_reason,
                    document_anchors=document_anchors,
                    missed_text=missed_text[:1500] if missed_text else "(no text available)",
                    failed_regex=suggestion.candidate_regex or "",
                    allowed_types=", ".join(ALLOWED_SUGGESTION_TYPES),
                    allowed_risks=", ".join(ALLOWED_RISK_LEVELS),
                )
                try:
                    retry_result = self._orch.generate_json(
                        role=self._role,
                        prompt=retry_prompt,
                        schema=RegexSuggestion,
                        subject_kind="parse_attempt",
                        subject_id=str(parse_attempt_id),
                        stage="regex_suggestion_retry",
                    )
                    if retry_result.status in ("ok", "fallback_used"):
                        retry_suggestion: RegexSuggestion = retry_result.result
                        if (retry_suggestion.candidate_regex or "").strip():
                            # Only accept the retry if it passes basic tests
                            retry_failure = self._test_candidate_regex(
                                retry_suggestion.candidate_regex or "",
                                missed_text,
                                anchors,
                            )
                            if not retry_failure:
                                suggestion = retry_suggestion
                                if suggestion.suggestion_type not in ALLOWED_SUGGESTION_TYPES:
                                    suggestion.suggestion_type = "regex_candidate"
                                if suggestion.risk not in ALLOWED_RISK_LEVELS:
                                    suggestion.risk = "medium"
                                if not (suggestion.target_profile or "").strip():
                                    suggestion.target_profile = parser_profile
                except Exception:
                    pass  # retry failed — keep original suggestion

        # Persist to DB
        self._persist_suggestion(suggestion, diagnosis_id, run_result.model or "unknown")

        # Export review artifact
        self._export_review_artifact(suggestion, diagnosis_id, parser_profile)

        return suggestion

    def generate_batch(
        self, diagnosis_rows: list[dict[str, Any]], limit: int = 10
    ) -> list[RegexSuggestion]:
        """Generate suggestions for multiple diagnoses."""
        results: list[RegexSuggestion] = []
        for row in diagnosis_rows[:limit]:
            try:
                suggestion = self.generate_suggestion(row)
                if suggestion:
                    results.append(suggestion)
            except Exception:
                continue
        return results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_missed_text(
        self, row: dict[str, Any], parse_meta: dict[str, Any]
    ) -> str:
        """Get rate-relevant text that the parser missed.

        Prefers section-aware text selection (Phase 6D) when
        ``document_sections`` has been populated for the document, falling
        back to page-level metadata scoring and finally raw full_text.
        """
        # Try to get from parse_attempt metadata first
        missed = parse_meta.get("missed_text", "")
        if missed:
            return _regex_context_excerpt(str(missed))

        source_pdf = row.get("source_pdf", "")
        if source_pdf:
            # Prefer section-aware text (Phase 6D) over page scoring
            db_text = self._fetch_section_aware_text(source_pdf)
            if db_text:
                return db_text

        text_metrics = parse_meta.get("text_metrics") if isinstance(parse_meta, dict) else None
        if isinstance(text_metrics, dict) and text_metrics.get("full_text"):
            return _regex_context_excerpt(str(text_metrics.get("full_text") or ""))

        return ""

    def _fetch_section_aware_text(
        self, source_pdf: str, *, max_chars: int = 6000
    ) -> str:
        """Fetch text from the best rate-relevant section, with fallback."""
        return fetch_section_aware_text(
            self._db_path, source_pdf, max_chars=max_chars,
        )

    def _fetch_rate_relevant_text(
        self, source_pdf: str, *, max_chars: int = 6000
    ) -> str:
        """Fetch and score pages using DB classification signals (legacy)."""
        return _regex_context_excerpt(
            fetch_rate_relevant_text(self._db_path, source_pdf, max_chars=max_chars)
        )

    @staticmethod
    def _score_page_from_metadata(
        meta_json: str,
        page_number: int,
        tariff_page_set: set[int],
        doc_anchor_codes: set[str],
        *,
        page_text: str = "",
    ) -> int:
        """Score a page for rate relevance using pipeline classification signals."""
        try:
            meta = json.loads(meta_json)
        except (json.JSONDecodeError, TypeError):
            meta = {}

        score = 0

        # Strong signals — these pages are almost certainly rate sheets
        if meta.get("has_leaf_header"):
            score += 30
        if meta.get("has_schedule_heading"):
            score += 25
        if meta.get("has_revised_header"):
            score += 10

        # Vocabulary densities (0.0–1.0 range typically)
        tariff_density = float(meta.get("tariff_vocab_density") or 0.0)
        procedural_density = float(meta.get("procedural_vocab_density") or 0.0)
        numeric_density = float(meta.get("numeric_density") or 0.0)
        table_density = float(meta.get("table_like_density") or 0.0)

        score += int(tariff_density * 20)
        score -= int(procedural_density * 10)  # penalize procedural text
        score += int(numeric_density * 15)
        score += int(table_density * 10)

        # Extracted codes — pages with schedule/rider codes are likely rate pages
        leaf_nos = meta.get("extracted_leaf_nos") or []
        schedule_codes = meta.get("extracted_schedule_codes") or []
        if isinstance(leaf_nos, list) and leaf_nos:
            score += 5 * len(leaf_nos)
        if isinstance(schedule_codes, list) and schedule_codes:
            score += 5 * len(schedule_codes)

        # Bonus: extracted codes match the document's known anchor codes
        if doc_anchor_codes:
            for code in schedule_codes:
                if isinstance(code, str) and code.upper() in doc_anchor_codes:
                    score += 10
                    break

        # Effective date phrase — often on the same page as rates
        if meta.get("has_effective_date_phrase"):
            score += 5

        # Span-level bonus — pages inside a "tariff" span
        if page_number in tariff_page_set:
            score += 10

        # Text-content signals — actual rate values on the page
        if page_text:
            lower = page_text.lower()
            # Dollar or cent amounts are a strong rate signal
            dollar_count = page_text.count("$")
            cent_count = page_text.count("¢")
            if dollar_count >= 3:
                score += 15
            elif dollar_count >= 1:
                score += 8
            if cent_count >= 3:
                score += 12
            elif cent_count >= 1:
                score += 6
            # Rate-unit keywords near numbers
            if any(term in lower for term in ("$/kwh", "$/kw", "¢/kwh", "cents/kwh", "per kwh", "per kw", "per watt", "per month")):
                score += 10

        return score

    def _load_tariff_span_pages(self, source_pdf: str) -> set[int]:
        """Return the set of page numbers that fall within 'tariff' spans."""
        tariff_pages: set[int] = set()
        try:
            conn = sqlite3.connect(str(self._db_path))
            spans = conn.execute(
                """
                SELECT start_page, end_page
                FROM ncuc_span_artifacts
                WHERE source_pdf = ? AND doc_type = 'tariff'
                """,
                (source_pdf,),
            ).fetchall()
            conn.close()
            for start_page, end_page in spans:
                for p in range(int(start_page), int(end_page) + 1):
                    tariff_pages.add(p)
        except Exception:
            pass
        return tariff_pages

    def _scan_rate_candidates(self, text: str) -> str:
        """Pre-scan text for likely rate values to guide the LLM.

        Returns a formatted string listing discovered rate candidates so the
        model knows what it's looking for before writing a regex.
        """
        if not text or len(text.strip()) < 20:
            return "(no rate candidates detected — text too short)"

        candidates: list[str] = []

        # Dollar amounts near rate units: $X.XX/kWh, $X.XX per kWh, etc.
        dollar_rate = re.compile(
            r"\$(\d+\.\d{2,})\s*(?:per\s*|/)\s*(kWh|kW|month|day|kW-hr)",
            re.IGNORECASE,
        )
        for m in dollar_rate.finditer(text):
            candidates.append(f"${m.group(1)}/{m.group(2)}")

        # Cents-per-kWh: X.XXX cents/¢ per kWh
        cent_rate = re.compile(
            r"(\d+\.\d{2,})\s*(?:cents?|¢|c\b)\s*(?:per\s*|/)\s*(kWh|kW-hr)",
            re.IGNORECASE,
        )
        for m in cent_rate.finditer(text):
            candidates.append(f"{m.group(1)}¢/{m.group(2)}")

        # Fixed monthly charges: $X.XX (near "Basic" or "Facilities" or "month")
        fixed_monthly = re.compile(
            r"(?:Basic\s+Facilities\s+Charge|Basic\s+Customer\s+Charge|monthly\s+charge)"
            r"[\s\S]{0,80}?\$(\d+\.\d{2})",
            re.IGNORECASE,
        )
        for m in fixed_monthly.finditer(text):
            candidates.append(f"${m.group(1)}/month (fixed charge)")

        # kW demand charges
        demand_rate = re.compile(
            r"\$(\d+\.\d{2})\s*(?:per\s*|/)\s*kW\b",
            re.IGNORECASE,
        )
        for m in demand_rate.finditer(text):
            candidates.append(f"${m.group(1)}/kW")

        if not candidates:
            # Fallback: just scan for dollar amounts
            dollar_any = re.compile(r"\$(\d+\.\d{2,})")
            raw = dollar_any.findall(text)
            if raw:
                unique = list(dict.fromkeys(raw))[:8]
                candidates = [f"${v} (unknown unit)" for v in unique]
            else:
                return "(no rate candidates detected — no $X.XX patterns found in text)"

        # Deduplicate, limit to 10
        unique_candidates = list(dict.fromkeys(candidates))[:10]
        return "\n".join(f"  - {c}" for c in unique_candidates)

    def _test_candidate_regex(
        self,
        candidate_regex: str,
        missed_text: str,
        anchors: dict[str, list[str]],
    ) -> str:
        """Test a generated regex against document text.

        Returns an empty string if the regex passes basic checks, or a
        human-readable failure reason if it doesn't.
        """
        # Check 1: compile
        try:
            pattern = re.compile(candidate_regex, re.IGNORECASE | re.MULTILINE)
        except re.error as e:
            return f"regex failed to compile: {e}"

        # Check 2: anchor presence
        if anchors and not regex_contains_anchor(candidate_regex, anchors):
            return (
                "candidate regex lacks a document-specific schedule/rider anchor — "
                "add one of the listed schedule codes, rider codes, or leaf numbers "
                "to scope the regex"
            )

        # Check 3: matches against text
        if missed_text:
            try:
                matches = pattern.findall(missed_text)
                if not matches:
                    return (
                        "regex produced ZERO matches against the document text. "
                        "The pattern may be too rigid — allow for OCR noise, "
                        "variable whitespace, and line breaks. IMPORTANT: replace "
                        "ALL literal spaces between tokens with \\\\s+ (e.g. "
                        "\"Payment:\\\\s+\\\\$\" not \"Payment: \\\\$\") because "
                        "OCR often inserts extra spaces. Also use [\\\\s\\\\S]*? "
                        "instead of literal newlines between sections."
                    )
            except Exception:
                pass

        # Check 4: if matches found, verify at least one looks like a numeric
        # rate value. Accept any decimal (1+ fractional digits) — cents-per-
        # kWh rates like "5.0¢" have only one decimal but are valid; the
        # previous "2+ decimals" rule rejected legitimate cents captures.
        if missed_text:
            try:
                matches = pattern.findall(missed_text)
                if matches:
                    rate_like = False
                    for m in matches[:20]:
                        piece = " ".join(str(p) for p in (m if isinstance(m, tuple) else (m,)))
                        if re.search(r"\d+(?:\.\d+)?", piece):
                            rate_like = True
                            break
                    if not rate_like:
                        return (
                            f"regex matched {len(matches)} times but none contain "
                            "a numeric value. Adjust the capturing group to capture "
                            "the rate digits (e.g. (\\d+\\.\\d+) for dollars or "
                            "(\\d+(?:\\.\\d+)?) for cents)."
                        )
            except Exception:
                pass

        return ""  # no failure — regex passes basic checks

    def _persist_suggestion(
        self,
        suggestion: RegexSuggestion,
        diagnosis_id: int,
        model: str,
    ) -> None:
        """Write suggestion to llm_regex_suggestions. Best-effort."""
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute(
                """
                INSERT INTO llm_regex_suggestions
                    (diagnosis_id, suggestion_type, target_profile, target_field,
                     missed_text, likely_issue, candidate_regex, candidate_normalization,
                     expected_unit, risk, positive_test_cases_json, negative_test_cases_json,
                     confidence, model, model_role, prompt_version, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    diagnosis_id,
                    suggestion.suggestion_type,
                    suggestion.target_profile or "",
                    suggestion.target_field or "",
                    (suggestion.missed_text or "")[:4000],
                    suggestion.likely_issue or "",
                    suggestion.candidate_regex or "",
                    suggestion.candidate_normalization or "",
                    suggestion.expected_unit or "",
                    suggestion.risk,
                    json.dumps([tc.model_dump() for tc in suggestion.positive_test_cases]),
                    json.dumps([tc.model_dump() for tc in suggestion.negative_test_cases]),
                    suggestion.confidence,
                    model,
                    self._role,
                    "v1",
                    "pending_review",
                ),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def _export_review_artifact(
        self,
        suggestion: RegexSuggestion,
        diagnosis_id: int,
        parser_profile: str,
    ) -> None:
        """Write a human-reviewable JSON artifact to docs/reports/regex_suggestions/."""
        try:
            report_dir = Path("docs/reports/regex_suggestions")
            report_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now(timezone.utc).strftime("%Y_%m_%d")
            safe_profile = parser_profile.replace("/", "_").replace("\\", "_")[:60]
            filename = f"{timestamp}_{safe_profile}_d{diagnosis_id}.json"
            filepath = report_dir / filename

            artifact = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "diagnosis_id": diagnosis_id,
                "profile": parser_profile,
                "suggestion": suggestion.model_dump(),
                "status": "pending_review",
                "review_notes": "",
            }
            filepath.write_text(json.dumps(artifact, indent=2, default=str), encoding="utf-8")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Shared page-scoring utilities (module-level for reuse across modules)
# ---------------------------------------------------------------------------


def score_page_from_metadata(
    meta_json: str,
    page_number: int,
    tariff_page_set: set[int],
    doc_anchor_codes: set[str],
    *,
    page_text: str = "",
) -> int:
    """Score a page for rate relevance using pipeline classification signals.

    Uses the pre-computed signals stored in ``ncuc_page_artifacts.metadata_json``
    (has_leaf_header, has_schedule_heading, tariff_vocab_density, etc.) plus
    span-level ``doc_type`` to identify rate-relevant pages.

    Returns an integer score — higher = more likely to contain rate content.
    """
    return RegexSuggestionGenerator._score_page_from_metadata(
        meta_json, page_number, tariff_page_set, doc_anchor_codes, page_text=page_text
    )


def load_tariff_span_pages(db_path: Path | str, source_pdf: str) -> set[int]:
    """Return the set of page numbers that fall within 'tariff' spans."""
    tariff_pages: set[int] = set()
    try:
        conn = sqlite3.connect(str(db_path))
        spans = conn.execute(
            """
            SELECT start_page, end_page
            FROM ncuc_span_artifacts
            WHERE source_pdf = ? AND doc_type = 'tariff'
            """,
            (source_pdf,),
        ).fetchall()
        conn.close()
        for start_page, end_page in spans:
            for p in range(int(start_page), int(end_page) + 1):
                tariff_pages.add(p)
    except Exception:
        pass
    return tariff_pages


def fetch_rate_relevant_text(
    db_path: Path | str,
    source_pdf: str,
    *,
    max_chars: int = 6000,
) -> str:
    """Fetch and score pages using DB classification signals.

    Queries ``ncuc_page_artifacts.metadata_json`` for per-page signals plus
    ``ncuc_span_artifacts`` for span-level doc_type, scores each page, and
    returns the highest-scoring pages concatenated in page-number order.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        pages = conn.execute(
            """
            SELECT pa.page_number, pa.text_content, pa.metadata_json
            FROM ncuc_page_artifacts pa
            WHERE pa.source_pdf = ?
            ORDER BY pa.page_number
            """,
            (source_pdf,),
        ).fetchall()
    except Exception:
        return ""
    finally:
        conn.close()

    if not pages:
        return ""

    # Deduplicate by page_number
    seen: set[int] = set()
    deduped: list[tuple[int, str, str]] = []
    for pn, text, meta in pages:
        if pn not in seen:
            seen.add(pn)
            deduped.append((pn, text or "", meta or "{}"))

    tariff_page_set = load_tariff_span_pages(db_path, source_pdf)

    # Load document anchor codes for cross-referencing
    doc_anchor_codes: set[str] = set()
    try:
        anchors = fetch_document_anchors(db_path, source_pdf)
        for key in ("schedule_codes", "rider_codes"):
            doc_anchor_codes.update(c.upper() for c in anchors.get(key, []))
    except Exception:
        pass

    scored: list[tuple[int, str, int]] = []
    for pn, text, meta_json in deduped:
        score = score_page_from_metadata(
            meta_json, pn, tariff_page_set, doc_anchor_codes, page_text=text
        )
        scored.append((score, text, pn))

    max_score = max((s for s, _, _ in scored), default=0)
    # Only take pages that score at least half the top score
    threshold = max(max_score // 2, 15) if max_score > 0 else 15
    score_map = {pn: s for s, _, pn in scored}
    selected = [
        (pn, t) for s, t, pn in scored
        if s >= threshold
    ]
    # Sort highest-scoring pages first so rate-relevant content fills the budget
    selected.sort(key=lambda kv: -score_map[kv[0]])

    parts: list[str] = []
    total = 0
    for _pn, text in selected:
        if total >= max_chars:
            break
        parts.append(text)
        total += len(text)
    return "\n".join(parts)[:max_chars]


def fetch_section_aware_text(
    db_path: Path | str,
    source_pdf: str,
    *,
    max_chars: int = 6000,
) -> str:
    """Fetch text from the highest-confidence rate-relevant section of a document.

    When ``document_sections`` has been populated (Phase 6B), uses section
    boundaries to select pages from the best rate_schedule or rider section
    instead of the generic page-scoring heuristic. Falls back to
    ``fetch_rate_relevant_text`` when no sections exist.
    """
    try:
        from .document_sections import fetch_rate_sections, ensure_schema

        ensure_schema(db_path)
        rate_sections = fetch_rate_sections(db_path, source_pdf, min_confidence=0.3)
        if not rate_sections:
            return fetch_rate_relevant_text(
                db_path, source_pdf, max_chars=max_chars,
            )

        # Take the highest-confidence rate section
        best = rate_sections[0]

        # Load pages within the section's boundaries
        conn = sqlite3.connect(str(db_path))
        try:
            pages = conn.execute(
                """SELECT text_content
                   FROM ncuc_page_artifacts
                   WHERE source_pdf = ?
                     AND page_number BETWEEN ? AND ?
                   ORDER BY page_number""",
                (source_pdf, best.start_page, best.end_page),
            ).fetchall()
        finally:
            conn.close()

        if not pages:
            return fetch_rate_relevant_text(
                db_path, source_pdf, max_chars=max_chars,
            )

        text = "\n".join(p[0] or "" for p in pages)
        return _regex_context_excerpt(text, limit=max_chars)

    except Exception:
        return fetch_rate_relevant_text(
            db_path, source_pdf, max_chars=max_chars,
        )
