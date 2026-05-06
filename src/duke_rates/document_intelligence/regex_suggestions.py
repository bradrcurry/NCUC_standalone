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

## Text the current parser MISSED:
```
{missed_text}
```

## Examples of SUCCESSFUL matches from the same profile:
{successful_examples}

## Instructions:
1. Choose suggestion_type ONLY from: {allowed_types}
2. Risk level ONLY from: {allowed_risks}
3. If suggesting a regex, provide candidate_regex that captures the missed value(s).
   The regex MUST contain at least one of the document-specific anchors above
   (a schedule code, rider code, or distinctive title fragment). A regex like
   `\\$\\d+\\.\\d+\\s*/kWh` is too generic — it must be scoped, e.g.
   `Schedule\\s+RES-28[\\s\\S]*?\\$(\\d+\\.\\d+)\\s*/kWh`.
4. If the issue is text normalization (OCR artifacts, symbol noise), provide candidate_normalization instead.
5. Include 2-5 positive_test_cases (lines that SHOULD match) and 2-5 negative_test_cases
   (lines that should NOT match). Negative cases SHOULD include a line from a
   different schedule (e.g. if anchor is RES-28, include a SGS or LGS line).
6. Quote the missed_text exactly from the document excerpt.
7. Expected_unit should be the physical or billing unit (kWh, $/kWh, $/month, etc.) or empty if unknown.
8. Include confidence from 0.0 to 1.0 based on how specific, evidence-backed, and testable the suggestion is.
9. Return an EMPTY candidate_regex if you cannot construct a plausible pattern — do not invent regexes.

Respond with a single JSON object matching the required schema. No other text."""


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

    def generate_suggestion(self, diagnosis_row: dict[str, Any]) -> RegexSuggestion | None:
        """Generate a regex/normalization suggestion for one diagnosis. Never raises."""
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

        prompt = _SUGGESTION_SYSTEM_PROMPT.format(
            failure_type=failure_type,
            target_field=target_field or "(unknown)",
            target_profile=parser_profile,
            current_patterns=current_patterns,
            expected_schema=expected_schema,
            document_anchors=document_anchors,
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
        # Without this, the validation harness's profile-aware tests cannot run
        # and every suggestion lands in needs_human_review by default.
        if not (suggestion.target_profile or "").strip():
            suggestion.target_profile = parser_profile

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
        """Get the text that the parser missed."""
        # Try to get from parse_attempt metadata
        missed = parse_meta.get("missed_text", "")
        if missed:
            return _regex_context_excerpt(str(missed))

        text_metrics = parse_meta.get("text_metrics") if isinstance(parse_meta, dict) else None
        if isinstance(text_metrics, dict) and text_metrics.get("full_text"):
            return _regex_context_excerpt(str(text_metrics.get("full_text") or ""))

        # Try to get source text from page artifacts
        source_pdf = row.get("source_pdf", "")
        if source_pdf:
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            try:
                pages = conn.execute(
                    """
                    SELECT pa.text_content
                    FROM ncuc_page_artifacts pa
                    WHERE pa.source_pdf = ?
                    ORDER BY pa.page_number
                    LIMIT 5
                    """,
                    (source_pdf,),
                ).fetchall()
                if pages:
                    return _regex_context_excerpt("\n".join(p["text_content"] or "" for p in pages))
            except Exception:
                pass
            finally:
                conn.close()

        return ""

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
