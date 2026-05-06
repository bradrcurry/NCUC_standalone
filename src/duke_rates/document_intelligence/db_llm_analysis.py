"""LLM-assisted database intelligence: summarization and safe SQL generation.

Uses the Phase 2.5 ``OllamaOrchestrator`` for structured LLM calls.
All LLM output is advisory — never auto-modifies data or code.

Read-only.  Deterministic reports are always the source of truth.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Pydantic models — LLM response schemas
# ---------------------------------------------------------------------------


class KeyFinding(BaseModel):
    severity: str = "medium"  # critical | high | medium | low
    finding: str = ""
    affected_count: int = 0
    specific_examples: list[str] = Field(default_factory=list)


class CoverageAnalysis(BaseModel):
    total_findings: int = 0
    by_severity: dict[str, int] = Field(default_factory=dict)
    gap_categories: list[str] = Field(default_factory=list)
    affected_families: list[str] = Field(default_factory=list)


class HighValueAction(BaseModel):
    priority: int = 99  # 1 = highest
    action: str = ""
    expected_impact: str = ""
    effort_estimate: str = "medium"  # low | medium | high
    affected_count: int = 0


class SuggestedQuery(BaseModel):
    description: str = ""
    sql: str = ""
    expected_result: str = ""


class IntelligenceSummaryResponse(BaseModel):
    summary: str  # required — an empty summary means the LLM produced garbage
    key_findings: list[KeyFinding] = Field(default_factory=list)
    likely_root_causes: list[str] = Field(default_factory=list)
    coverage_analysis: CoverageAnalysis | None = None
    high_value_actions: list[HighValueAction] = Field(default_factory=list)
    suggested_queries: list[SuggestedQuery] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class SqlGenerationResult(BaseModel):
    question: str = ""
    generated_sql: str = ""
    explanation: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# SQL safety validator
# ---------------------------------------------------------------------------

# Keywords that MUST NOT appear in a generated query
DISALLOWED_KEYWORDS: list[str] = [
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
    "TRUNCATE", "REPLACE", "ATTACH", "DETACH", "PRAGMA",
    "VACUUM", "REINDEX", "GRANT", "REVOKE", "EXECUTE",
    "IMPORT", "EXPORT", "LOAD", "DUMP", "RESTORE",
]

# Tables that a generated query may reference
ALLOWED_TABLES: set[str] = {
    "bill_component_observations",
    "bill_statements",
    "database_intelligence_runs",
    "dep_provisional_rider_totals",
    "dep_provisional_rider_components",
    "docling_artifacts",
    "document_classifications",
    "document_embeddings",
    "document_fingerprints",
    "document_fingerprints_v2",
    "document_types",
    "documents",
    "eia_state_rates",
    "eia_retail_sales",
    "historical_documents",
    "historical_leads",
    "historical_processing_runs",
    "historical_reprocess_queue",
    "llm_candidate_rate_extractions",
    "llm_parse_diagnostics",
    "llm_regex_suggestions",
    "llm_regex_validation_results",
    "missing_doc_remediation_runs",
    "ncuc_discovery_records",
    "ncuc_ingest_segments",
    "ncuc_page_artifacts",
    "ncuc_span_artifacts",
    "ocr_artifacts",
    "ocr_processing_queue",
    "ollama_model_runs",
    "parse_attempt_logs",
    "parse_review_outcomes",
    "parse_results",
    "regulatory_docket_leads",
    "rider_applicability",
    "rider_descriptions",
    "rider_line_items",
    "rider_summary_blocks",
    "tariff_charges",
    "tariff_families",
    "tariff_versions",
    "workflow_action_receipts",
}

MAX_ROWS_HARD_LIMIT = 100


def validate_sql_safety(sql: str) -> tuple[bool, str | None]:
    """Screen a SQL string for safety before execution.

    Returns ``(is_safe, error_message)``.
    """
    if not sql or not sql.strip():
        return False, "Empty SQL string."

    stripped = sql.strip()
    upper = stripped.upper()

    # 1. Must start with SELECT or WITH
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        return False, "Query must start with SELECT or WITH (CTE only)."

    # 2. No disallowed keywords (word-boundary match)
    for kw in DISALLOWED_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper):
            return False, f"Disallowed keyword detected: {kw}"

    # 3. No multiple statements (semicolons outside string literals)
    no_strings = re.sub(r"'[^']*'", "", stripped)
    no_strings = re.sub(r'"[^"]*"', "", no_strings)
    # Remove trailing semicolon for the check
    cleaned = no_strings.rstrip(";").strip()
    if ";" in cleaned:
        return False, "Multiple statements are not allowed."

    return True, None


def enforce_limit(sql: str, max_rows: int = 25) -> str:
    """Ensure the SQL has a LIMIT clause that doesn't exceed the hard cap."""
    cap = min(max_rows, MAX_ROWS_HARD_LIMIT)
    stripped = sql.strip().rstrip(";").rstrip()
    upper = stripped.upper()

    limit_match = re.search(r"\bLIMIT\s+(\d+)", upper)
    if limit_match:
        existing_limit = int(limit_match.group(1))
        if existing_limit > cap:
            return re.sub(
                r"\bLIMIT\s+\d+",
                f"LIMIT {cap}",
                stripped,
                flags=re.IGNORECASE,
            )
        return stripped
    else:
        return f"{stripped} LIMIT {cap}"


def execute_safe_query(
    conn: sqlite3.Connection,
    sql: str,
    *,
    max_rows: int = 25,
    timeout_s: int = 30,
) -> dict[str, Any]:
    """Validate, guard, and execute a read-only query.

    Returns ``{status, row_count, rows, error_message, duration_ms}``.
    """
    t0 = time.monotonic()

    # Safety screen
    is_safe, err = validate_sql_safety(sql)
    if not is_safe:
        return {
            "status": "safety_blocked",
            "row_count": 0,
            "rows": [],
            "error_message": err,
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }

    # Enforce LIMIT
    guarded_sql = enforce_limit(sql, max_rows)

    try:
        conn.execute(f"PRAGMA query_timeout={timeout_s * 1000}")
        cursor = conn.execute(guarded_sql)
        rows = [dict(r) for r in cursor.fetchall()]
        cursor.close()
        return {
            "status": "ok",
            "row_count": len(rows),
            "rows": rows,
            "error_message": None,
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }
    except Exception as exc:
        return {
            "status": "execution_error",
            "row_count": 0,
            "rows": [],
            "error_message": str(exc),
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }


# ---------------------------------------------------------------------------
# LLM summarization
# ---------------------------------------------------------------------------

SUMMARIZATION_SYSTEM_PROMPT = """\
You are analyzing a corpus intelligence report for the NCUC (North Carolina
Utilities Commission) tariff document database. The report contains findings
across these categories:

1. Missing Versions — gaps in tariff/rider version timelines
2. Unknown Documents — documents with UNKNOWN classification, grouped by cluster
3. Low Quality Parses — weak/empty parse attempts with zero or low charge counts
4. Stale Artifacts — documents missing evidence or stuck in reprocess queues
5. Duplicate Documents — file-hash duplicates detected by fingerprints
6. Family Lineage Gaps — broken or inconsistent family->version->document chains
7. Docket Coverage — document counts per docket, year, and category

Below is a compact JSON summary of findings (counts and representative examples).

Provide a structured analysis with:
- Executive summary (2-3 sentences)
- Key findings ranked by severity (critical/high/medium/low)
- Likely root causes for the most common patterns
- Coverage analysis: which areas have the most gaps?
- High-value actions: top 3-5 specific, scoped things to fix, with effort estimates
- Suggested follow-up SQL queries for deeper investigation

Be specific. Reference family keys, docket numbers, and counts when available.
"""


def _compact_report(raw: dict[str, Any]) -> dict[str, Any]:
    """Strip sample rows from a report, keeping only counts and 1-2 exemplars."""
    compact: dict[str, Any] = {
        "generated_at": raw.get("generated_at", ""),
        "config": raw.get("config", {}),
        "summary_counts": raw.get("summary_counts", {}),
        "total_findings": raw.get("total_findings", 0),
    }
    for section in [
        "missing_versions",
        "unknown_documents",
        "low_quality_parses",
        "stale_artifacts",
        "duplicate_documents",
        "family_lineage_gaps",
        "docket_coverage",
    ]:
        sec = raw.get(section, {})
        summary = sec.get("summary", {})
        rows = sec.get("rows", [])
        # Only keep summary stats and 1 representative row (keys only, no full paths)
        exemplars: list[dict[str, Any]] = []
        for r in rows[:2]:
            slim = {}
            for k, v in r.items():
                if isinstance(v, list):
                    slim[k] = f"[{len(v)} items]"
                elif isinstance(v, str) and len(v) > 120:
                    slim[k] = v[:120] + "..."
                else:
                    slim[k] = v
            exemplars.append(slim)
        compact[section] = {
            "summary": summary,
            "sample_count": len(rows),
            "exemplars": exemplars,
        }
    return compact


def _extract_json_from_text(text: str) -> dict[str, Any] | None:
    """Try to extract a JSON object from a text response.

    Handles markdown code fences, leading/trailing text, and
    the occasional ``json`` language tag.
    """
    if not text or not text.strip():
        return None

    # Prefer fenced JSON block
    fence_m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_m:
        try:
            return json.loads(fence_m.group(1))
        except json.JSONDecodeError:
            pass

    # Otherwise find the outermost { } pair
    start = text.find("{")
    if start == -1:
        return None
    # Count braces to find matching close
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _normalize_key_findings(raw: Any) -> list[dict[str, Any]]:
    """Convert various key_findings shapes into the expected list-of-dicts."""
    if isinstance(raw, list):
        # Remap common alternative field names
        result: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict):
                entry: dict[str, Any] = {}
                entry["severity"] = str(item.get("severity", "medium")).lower()
                entry["finding"] = str(
                    item.get("finding")
                    or item.get("details")
                    or item.get("description")
                    or item.get("category", "")
                )
                entry["affected_count"] = int(item.get("affected_count", item.get("count", 0)))
                if "specific_examples" in item:
                    entry["specific_examples"] = item["specific_examples"]
                result.append(entry)
            elif isinstance(item, str):
                result.append({"finding": item})
        return result
    if isinstance(raw, dict):
        items: list[dict[str, Any]] = []
        for severity, entries in raw.items():
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        entry.setdefault("severity", severity)
                        items.append(entry)
                    elif isinstance(entry, str):
                        items.append({"severity": severity, "finding": entry})
        return _normalize_key_findings(items)  # Re-process to remap field names
    return []


def _normalize_root_causes(raw: Any) -> list[str]:
    """Convert various likely_root_causes shapes into a flat list of strings."""
    if isinstance(raw, list):
        causes: list[str] = []
        for item in raw:
            if isinstance(item, str):
                causes.append(item)
            elif isinstance(item, dict):
                # Prefer 'description', 'cause', 'finding', or first string value
                for key in ("description", "cause", "finding", "reason"):
                    if key in item and isinstance(item[key], str):
                        causes.append(item[key])
                        break
                else:
                    causes.append(str(item))
        return causes
    if isinstance(raw, dict):
        causes = []
        for _category, entries in raw.items():
            if isinstance(entries, list):
                causes.extend(_normalize_root_causes(entries))
            elif isinstance(entries, str):
                causes.append(entries)
        return causes
    return []


def _normalize_actions(raw: Any) -> list[dict[str, Any]]:
    """Convert various high_value_actions shapes into list-of-dicts."""
    if isinstance(raw, list):
        result: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict):
                entry: dict[str, Any] = {}
                entry["priority"] = int(item.get("priority", 99))
                entry["action"] = str(item.get("action", item.get("description", item.get("title", ""))))
                entry["expected_impact"] = str(
                    item.get("expected_impact", item.get("description", item.get("impact", "")))
                )
                entry["effort_estimate"] = str(item.get("effort_estimate", "medium"))
                entry["affected_count"] = int(item.get("affected_count", item.get("count", 0)))
                result.append(entry)
            elif isinstance(item, str):
                result.append({"action": item})
        return result
    if isinstance(raw, dict):
        if "actions" in raw and isinstance(raw["actions"], list):
            return _normalize_actions(raw["actions"])
        items: list[dict[str, Any]] = []
        for key, entry in raw.items():
            if isinstance(entry, dict):
                entry.setdefault("action", key)
                items.append(entry)
        return _normalize_actions(items)
    return []


def _normalize_queries(raw: Any) -> list[dict[str, Any]]:
    """Convert various suggested_queries shapes into list-of-dicts."""
    if isinstance(raw, list):
        result: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict):
                entry: dict[str, Any] = {}
                entry["description"] = str(item.get("description", item.get("title", "")))
                entry["sql"] = str(item.get("sql", item.get("query", "")))
                entry["expected_result"] = str(item.get("expected_result", item.get("result", "")))
                result.append(entry)
            elif isinstance(item, str):
                result.append({"description": item})
        return result
    if isinstance(raw, dict) and "queries" in raw and isinstance(raw["queries"], list):
        return _normalize_queries(raw["queries"])
    return []


def _normalize_confidence(raw: Any) -> float:
    """Parse confidence from various representations (float, string, etc.)."""
    if isinstance(raw, (int, float)):
        return float(max(0.0, min(1.0, raw)))
    if isinstance(raw, str):
        mapping = {"high": 0.85, "medium": 0.6, "low": 0.35, "none": 0.0}
        return mapping.get(raw.lower().strip(), 0.5)
    return 0.5


def _validate_summary_response(parsed: dict[str, Any]) -> IntelligenceSummaryResponse | None:
    """Try to construct an IntelligenceSummaryResponse from a parsed dict.

    Handles common alternative formats that LLMs produce (e.g. key_findings
    as a dict keyed by severity instead of a list of KeyFinding objects).
    """
    try:
        return IntelligenceSummaryResponse(**parsed)
    except Exception:
        pass

    # Normalize common alternative structures
    try:
        normalized: dict[str, Any] = {}

        # summary
        if "summary" in parsed and isinstance(parsed["summary"], str):
            normalized["summary"] = parsed["summary"]

        # key_findings — accept dict-of-lists or raw list
        normalized["key_findings"] = _normalize_key_findings(parsed.get("key_findings", []))

        # likely_root_causes — accept dict-of-lists or raw list
        normalized["likely_root_causes"] = _normalize_root_causes(
            parsed.get("likely_root_causes", [])
        )

        # coverage_analysis — pass through if dict
        if "coverage_analysis" in parsed and isinstance(parsed["coverage_analysis"], dict):
            ca = parsed["coverage_analysis"]
            raw_gaps = ca.get("gap_categories", ca.get("gaps", ca.get("areas_with_gaps", [])))
            # Flatten list of dicts to strings if needed
            flat_gaps: list[str] = []
            for item in raw_gaps if isinstance(raw_gaps, list) else []:
                if isinstance(item, str):
                    flat_gaps.append(item)
                elif isinstance(item, dict):
                    flat_gaps.append(str(item.get("description", item.get("detail", item.get("category", str(item))))))
            raw_families = ca.get("affected_families", [])
            flat_families: list[str] = []
            for item in raw_families if isinstance(raw_families, list) else []:
                if isinstance(item, str):
                    flat_families.append(item)
                elif isinstance(item, dict):
                    flat_families.append(str(item.get("family_key", item.get("name", str(item)))))
            normalized["coverage_analysis"] = {
                "total_findings": ca.get("total_findings", 0),
                "by_severity": ca.get("by_severity", {}),
                "gap_categories": flat_gaps,
                "affected_families": flat_families,
            }

        # high_value_actions
        normalized["high_value_actions"] = _normalize_actions(
            parsed.get("high_value_actions", [])
        )

        # suggested_queries
        normalized["suggested_queries"] = _normalize_queries(
            parsed.get("suggested_queries", [])
        )

        # confidence
        normalized["confidence"] = _normalize_confidence(parsed.get("confidence", 0.5))

        if normalized.get("summary"):
            return IntelligenceSummaryResponse(**normalized)
    except Exception:
        pass

    return None


def summarize_report(
    orch: Any,
    report: dict[str, Any],
) -> IntelligenceSummaryResponse:
    """Feed a compact report to the LLM and return a structured summary.

    ``orch`` must be an ``OllamaOrchestrator`` instance.
    Tries JSON mode first; falls back to text generation if the model
    cannot produce valid JSON for this prompt.
    """
    compact = _compact_report(report)
    prompt = json.dumps(compact, indent=2, default=str)

    # If prompt is still large, trim further
    if len(prompt) > 12000:
        # Keep only summary counts + exemplar keys, drop full exemplars
        for section in compact:
            if isinstance(compact.get(section), dict) and "exemplars" in compact[section]:
                compact[section]["exemplars"] = compact[section]["exemplars"][:1]
        prompt = json.dumps(compact, indent=2, default=str)

    full_prompt = (
        f"{SUMMARIZATION_SYSTEM_PROMPT}\n\n"
        f"Report JSON:\n{prompt}\n\n"
        f"Return a JSON object matching the IntelligenceSummaryResponse schema "
        f"with summary, key_findings, likely_root_causes, coverage_analysis, "
        f"high_value_actions, suggested_queries, and confidence fields."
    )

    # --- Attempt 1: JSON mode ---
    result = orch.generate_json(
        role="balanced_classifier",
        prompt=full_prompt,
        schema=IntelligenceSummaryResponse,
        subject_kind="database_intelligence",
        subject_id=compact.get("generated_at", datetime.now(UTC).isoformat()),
        stage="summarization",
    )

    if result.status in ("ok", "fallback_used"):
        if isinstance(result.result, IntelligenceSummaryResponse):
            return result.result
        if isinstance(result.result, dict):
            validated = _validate_summary_response(result.result)
            if validated:
                return validated

    raw_payload = str(getattr(result, "raw_payload", "") or "")
    if raw_payload:
        # Maybe the raw payload itself contains valid JSON
        extracted = _extract_json_from_text(raw_payload)
        if extracted:
            validated = _validate_summary_response(extracted)
            if validated:
                return validated

    # --- Attempt 2: Text mode fallback ---
    text_result = orch.generate_text(
        role="balanced_classifier",
        prompt=(
            f"{full_prompt}\n\n"
            f"IMPORTANT: Output ONLY a valid JSON object. Do NOT wrap it in markdown "
            f"code fences. Start with {{ and end with }}."
        ),
        subject_kind="database_intelligence",
        subject_id=compact.get("generated_at", datetime.now(UTC).isoformat()),
        stage="summarization_text_fallback",
    )

    if text_result.status in ("ok", "fallback_used") and text_result.result:
        text = str(text_result.result)
        extracted = _extract_json_from_text(text)
        if extracted:
            validated = _validate_summary_response(extracted)
            if validated:
                return validated
        # Last resort — maybe the model returned something useful as plain text
        if len(text) > 20:
            return IntelligenceSummaryResponse(
                summary=text[:1000],
                confidence=0.3,
            )

    # --- All attempts failed ---
    status_msg = getattr(result, "status", "unknown")
    return IntelligenceSummaryResponse(
        summary=(
            f"LLM summarization failed (status={status_msg}). "
            f"Raw response: {raw_payload[:200]}"
        ),
        confidence=0.0,
    )


# ---------------------------------------------------------------------------
# SQL generation from natural language
# ---------------------------------------------------------------------------

SQL_GENERATION_SYSTEM_PROMPT = """\
You are a SQLite expert assistant for the Duke Energy / NCUC tariff document
database. Generate a valid, safe SQLite SELECT query that answers the user's
question.

Available tables (key columns only):
- historical_documents (id, family_key, title, state, company, effective_start,
  local_path, content_hash, leaf_no, category, kind, evidence_json, retrieved_at)
- tariff_families (id, family_key, state, company, schedule_code, family_type,
  title)
- tariff_versions (id, family_key, document_id, historical_document_id,
  effective_start, effective_end, source_type, confidence_score, docket_number,
  order_date, leaf_no)
- tariff_charges (id, version_id, family_key, charge_type, charge_label,
  rate_value, rate_unit, customer_class)
- ncuc_discovery_records (id, docket_number, utility, filing_title, filing_date,
  fetch_status, local_path, content_hash, doc_quality_tier)
- document_classifications (id, subject_kind, subject_id, stage, label,
  confidence, classifier, superseded_by)
- document_fingerprints_v2 (id, source_pdf, file_hash, page_count,
  cluster_signature_v1)
- parse_attempt_logs (id, source_pdf, parser_profile, status, confidence,
  charge_count)
- historical_reprocess_queue (id, historical_document_id, source_pdf, family_key,
  status, priority)
- tariff_versions.effective_start stores dates as ISO date strings 'YYYY-MM-DD'.
- document_classifications uses subject_kind='historical_document' with subject_id
  as the string representation of historical_documents.id.

Rules:
- ONLY SELECT statements.
- Always include a LIMIT clause (maximum 100).
- Prefer simple JOINs over subqueries when possible.
- Include meaningful column aliases.
- Include ORDER BY for deterministic output.

Return JSON with fields: question (the original question), generated_sql (the
SQL query), explanation (1 sentence explaining the query), confidence (0-1).
"""


def generate_sql(
    orch: Any,
    question: str,
) -> SqlGenerationResult:
    """Use the LLM to generate a SQL query from a natural-language question.

    ``orch`` must be an ``OllamaOrchestrator`` instance.
    """
    result = orch.generate_json(
        role="code_model",
        prompt=(
            f"{SQL_GENERATION_SYSTEM_PROMPT}\n\n"
            f"User question: {question}\n\n"
            f"Return ONLY the JSON with the SQL query."
        ),
        schema=SqlGenerationResult,
        subject_kind="ask_ncuc_db",
        subject_id=question[:80],
        stage="sql_generation",
    )

    if result.status in ("ok", "fallback_used"):
        if isinstance(result.result, SqlGenerationResult):
            return result.result
        if isinstance(result.result, dict):
            try:
                return SqlGenerationResult(**result.result)
            except Exception:
                pass

    return SqlGenerationResult(
        question=question,
        generated_sql="",
        explanation=f"LLM SQL generation failed: {result.status}",
        confidence=0.0,
    )


RESULT_SUMMARY_PROMPT = """\
The user asked: "{question}"

The following SQL was executed:
{sql}

Results ({row_count} rows returned):
{result_json}

Summarize these results in 1-3 sentences, directly answering the user's question.
Be specific about counts, names, and notable patterns.
"""


def summarize_query_results(
    orch: Any,
    question: str,
    sql: str,
    result_rows: list[dict[str, Any]],
) -> str:
    """Ask the LLM to summarize SQL query results in natural language."""
    sample = result_rows[:10]
    prompt = RESULT_SUMMARY_PROMPT.format(
        question=question,
        sql=sql,
        row_count=len(result_rows),
        result_json=json.dumps(sample, indent=2, default=str),
    )

    result = orch.generate_text(
        role="balanced_classifier",
        prompt=prompt,
        subject_kind="ask_ncuc_db",
        subject_id=question[:80],
        stage="result_summary",
    )

    if result.status in ("ok", "fallback_used") and result.result:
        return str(result.result)

    return f"Query returned {len(result_rows)} rows. Could not generate summary."


# ---------------------------------------------------------------------------
# Run logging
# ---------------------------------------------------------------------------


def log_run(
    conn: sqlite3.Connection,
    *,
    run_type: str,
    status: str = "started",
    question: str | None = None,
    generated_sql: str | None = None,
    safety_check: str | None = None,
    execution_status: str | None = None,
    row_count: int | None = None,
    report_sections: list[str] | None = None,
    summary_json: str | None = None,
    error_message: str | None = None,
    duration_ms: int = 0,
    config: dict[str, Any] | None = None,
    output_path: str | None = None,
    ollama_run_id: int | None = None,
) -> int:
    """Insert a row into ``database_intelligence_runs``.

    Returns the new row's id.
    """
    cursor = conn.execute(
        """
        INSERT INTO database_intelligence_runs
            (run_type, status, question, generated_sql, safety_check,
             execution_status, row_count, report_sections_json,
             summary_json, error_message, duration_ms, config_json,
             output_path, ollama_run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_type,
            status,
            question,
            generated_sql,
            safety_check,
            execution_status,
            row_count,
            json.dumps(report_sections) if report_sections else None,
            summary_json,
            error_message,
            duration_ms,
            json.dumps(config) if config else "{}",
            output_path,
            ollama_run_id,
        ),
    )
    conn.commit()
    return cursor.lastrowid or 0
