# NCUC Database Intelligence — MCP Server Design

**Status:** Planning only — no implementation.
**Phase:** 6.5
**Date:** 2026-05-01

## Overview

This document defines the tool interface for exposing the Phase 6.5 Database
Intelligence layer as an MCP (Model Context Protocol) server. The server would
provide Claude and other MCP clients with read-only access to corpus analytics,
safe natural-language querying, and report summarization.

All tools are **read-only** and **advisory** — they never modify the database,
parser code, or extraction results.

## Architecture

```
MCP Client (Claude, etc.)
    |
    v
ncuc-database-intelligence MCP Server
    |
    +-- deterministic_reports (SQLite queries)
    +-- llm_summarization (OllamaOrchestrator)
    +-- sql_generation (OllamaOrchestrator / code_model)
    +-- run_history (database_intelligence_runs table)
```

The server would reuse the existing `OllamaOrchestrator` and `database_reports`
modules. No new infrastructure.

## Tool Definitions

### 1. `ncuc_db_intelligence_report`

Run the full deterministic corpus analytics report.

**Input:**
```json
{
  "limit": 50,
  "family_key": "nc-progress-leaf-534",
  "docket": "E-2 Sub 1146",
  "since": "2024-01-01"
}
```
All inputs optional. Default `limit=50`.

**Output:**
```json
{
  "generated_at": "2026-05-01T12:00:00Z",
  "summary_counts": {
    "missing_versions": 12,
    "unknown_documents": 5,
    "low_quality_parses": 34,
    "stale_artifacts": 8,
    "duplicate_documents": 15,
    "family_lineage_gaps": 22,
    "docket_coverage": 45
  },
  "total_findings": 141,
  "sections": { "... per-section detail ..." }
}
```

**Safety:** Read-only SELECT queries. No mutation.

**Implementation:** Calls `build_database_intelligence_report()` from
`document_intelligence/database_reports.py`.

### 2. `ncuc_db_ask`

Safe natural-language SQL query against the NCUC corpus.

**Input:**
```json
{
  "question": "How many families have zero charges?",
  "max_rows": 25,
  "timeout_s": 30,
  "dry_run": false
}
```
`question` required.

**Output:**
```json
{
  "question": "How many families have zero charges?",
  "generated_sql": "SELECT ...",
  "status": "ok",
  "row_count": 15,
  "rows": [...],
  "summary": "15 families have no associated charges...",
  "duration_ms": 1234
}
```

**Safety guarantees:**
- SELECT-only enforcement (no INSERT/UPDATE/DELETE/DROP)
- Table whitelist — only known safe tables
- Automatic LIMIT cap at 100 rows
- Configurable query timeout
- Multi-statement detection and rejection
- SQL shown to caller before execution

**Implementation:** Uses `generate_sql()` + `execute_safe_query()` from
`document_intelligence/db_llm_analysis.py`. SQL generation uses
`code_model` role (qwen2.5-coder:14b).

### 3. `ncuc_db_summarize`

Feed a database intelligence report to an LLM for structured summarization.

**Input:**
```json
{
  "report_path": "docs/reports/database_intelligence/2026-05-01.json",
  "limit": 50
}
```
`report_path` optional — if omitted, generates a fresh report.

**Output:**
```json
{
  "summary": "The NCUC corpus shows...",
  "key_findings": [
    {
      "severity": "high",
      "finding": "2023 missing year gaps across 45 families",
      "affected_count": 45,
      "specific_examples": ["nc-progress-leaf-534", "nc-carolinas-rider-CEI"]
    }
  ],
  "likely_root_causes": [
    "Documents exist but lack effective_start dates",
    "Compliance bundles contain tariff sheets classified as UNKNOWN"
  ],
  "coverage_analysis": { "...": "..." },
  "high_value_actions": [
    {
      "priority": 1,
      "action": "Backfill effective_start for 99 historical documents",
      "expected_impact": "Closes 45 missing-version gaps",
      "effort_estimate": "medium",
      "affected_count": 99
    }
  ],
  "suggested_queries": [
    {
      "description": "Find documents with year 2023 in tariff_versions",
      "sql": "SELECT family_key, effective_start FROM tariff_versions WHERE effective_start LIKE '2023%'",
      "expected_result": "List of all 2023 versions"
    }
  ],
  "confidence": 0.85
}
```

**Implementation:** Uses `summarize_report()` from `db_llm_analysis.py`.
LLM: `balanced_classifier` role (qwen3:8b, fallback command-r).

**Safety:** LLM output is advisory. Never modifies data. Summary is persisted
separately from the deterministic report.

### 4. `ncuc_db_schema_explore`

List available tables, columns, and row counts for discovery.

**Input:**
```json
{
  "table_filter": "tariff"
}
```
Optional. If omitted, lists all tables.

**Output:**
```json
{
  "tables": [
    {
      "name": "tariff_versions",
      "row_count": 1328,
      "columns": [
        {"name": "id", "type": "INTEGER"},
        {"name": "family_key", "type": "TEXT"},
        {"name": "effective_start", "type": "TEXT"}
      ]
    }
  ]
}
```

**Implementation:** Queries `sqlite_master` and `PRAGMA table_info`. Read-only.

### 5. `ncuc_db_run_history`

List recent database intelligence runs for monitoring.

**Input:**
```json
{
  "limit": 10,
  "run_type": "overnight_full"
}
```
Optional filters.

**Output:**
```json
{
  "runs": [
    {
      "id": 42,
      "run_type": "overnight_full",
      "status": "completed",
      "duration_ms": 45000,
      "report_sections": ["missing_versions", "unknown_documents", "..."],
      "created_at": "2026-05-01T09:00:00"
    }
  ]
}
```

**Implementation:** Queries `database_intelligence_runs` table. Read-only.

## Safety Guarantees (All Tools)

| Constraint | Enforcement |
|---|---|
| Read-only | All tools use SELECT-only queries or read files |
| No mutation | No INSERT/UPDATE/DELETE/DROP/ALTER in any tool |
| Row limits | Max 100 rows per query; LIMIT enforced at SQL level |
| Timeouts | All queries have configurable timeout (default 30s) |
| Table whitelist | SQL generation only references known safe tables |
| LLM advisory only | LLM outputs never auto-modify code, data, or config |
| Audit trail | All runs logged to `database_intelligence_runs` |

## LLM Role Assignments

| Tool | Role | Model | Purpose |
|---|---|---|---|
| `ncuc_db_summarize` | `balanced_classifier` | qwen3:8b | Report summarization |
| `ncuc_db_ask` (SQL gen) | `code_model` | qwen2.5-coder:14b | SQL generation from NL |
| `ncuc_db_ask` (result summary) | `balanced_classifier` | qwen3:8b | Result summarization |

## Relationship to CLI Commands

Each MCP tool maps to an existing CLI command:

| MCP Tool | CLI Command |
|---|---|
| `ncuc_db_intelligence_report` | `report-database-intelligence-nc` |
| `ncuc_db_ask` | `ask-ncuc-db` |
| `ncuc_db_summarize` | `summarize-database-intelligence-nc` |
| `ncuc_db_schema_explore` | (new, not yet CLI) |
| `ncuc_db_run_history` | (new, not yet CLI) |

## Future Extensions (Phase 7+)

- `ncuc_db_find_similar` — semantic search across document embeddings
- `ncuc_db_trend_over_time` — time-series coverage/quality metrics
- `ncuc_db_compare_runs` — diff two intelligence report versions
- `ncuc_db_anomaly_alert` — configurable anomaly thresholds with notifications
- `ncuc_db_export_training` — export reviewed labels as training dataset

## Implementation Notes

When Phase 7 is ready for MCP implementation:

1. The server should be a standalone Python process using the `mcp` Python SDK.
2. It reuses `OllamaOrchestrator`, `database_reports`, and `db_llm_analysis`.
3. Authentication should use the existing NCID credentials from `.env`.
4. Tools that call LLMs should respect the existing prompt versioning and
   `ollama_model_runs` audit trail.
5. The server should NOT expose any tool that can modify the database.
