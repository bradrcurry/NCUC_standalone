# Agent Onboarding Guide
**Last Updated:** 2026-04-21
**Purpose:** Stable entry point for human operators and AI agents
**Target Read Time:** 5 minutes

This file is intentionally short. It is the router, not the full knowledge base.
Do not turn it into a session diary or a duplicate of deeper operator docs.

## First Read Order

Read these in order before broad repo exploration:

1. [README.md](/c:/Python/Duke/Standalone/README.md)
2. [source_of_truth_and_legacy_paths.md](/c:/Python/Duke/Standalone/docs/source_of_truth_and_legacy_paths.md)
3. [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md)
4. [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)
5. [python_environments.md](/c:/Python/Duke/Standalone/docs/python_environments.md)
6. [agent_task_routing.md](/c:/Python/Duke/Standalone/docs/agent_task_routing.md)
7. [agent_tool_use_policy.md](/c:/Python/Duke/Standalone/docs/agent_tool_use_policy.md)

**For tool and command discovery (read before writing any new helpers):**

8. [agent_tool_registry.json](/c:/Python/Duke/Standalone/docs/agent_tool_registry.json) — machine-readable supported/legacy tool classification
9. [agent_workflows.json](/c:/Python/Duke/Standalone/docs/agent_workflows.json) — machine-readable sanctioned workflow chains
10. [cli_command_reference.md](/c:/Python/Duke/Standalone/docs/cli_command_reference.md) — full command surface with known gaps

If you intend to extend document understanding, classification, or
fingerprinting (a recurring scope-creep target), read this BEFORE writing
any code:

10a. [document_intelligence_roadmap.md](/c:/Python/Duke/Standalone/docs/document_intelligence_roadmap.md)
   — phased plan with what already exists, what NOT to rebuild, and the
   smallest next increment.

Before closing work, read:

11. [agent_change_checklist.md](/c:/Python/Duke/Standalone/docs/agent_change_checklist.md)
12. [knowledge_capture_workflow.md](/c:/Python/Duke/Standalone/docs/knowledge_capture_workflow.md)

## What This Project Is

This repo discovers, downloads, parses, validates, and analyzes Duke Energy tariff documents.

The most active current path is the North Carolina NCUC historical pipeline:

1. discover candidate records
2. download PDFs
3. mine page and span evidence
4. link spans and documents to tariff families and versions
5. extract charges
6. review weak parses, OCR work, and targeted reprocessing

The database-backed pipeline state is the operational source of truth. Session summaries, dated notes, and external memory files are useful context, but they are not authoritative by themselves.

## Repo Map

| Area | Purpose |
|---|---|
| [src/duke_rates](/c:/Python/Duke/Standalone/src/duke_rates) | Main library, CLI, parsers, DB logic, historical pipeline |
| [docs/agent_tool_registry.json](/c:/Python/Duke/Standalone/docs/agent_tool_registry.json) | Machine-readable supported/legacy/alias classification for all agent-facing tools |
| [docs/agent_workflows.json](/c:/Python/Duke/Standalone/docs/agent_workflows.json) | Machine-readable sanctioned workflow catalog with tool chaining and entry conditions |
| [docs/cli_command_reference.md](/c:/Python/Duke/Standalone/docs/cli_command_reference.md) | Full CLI command reference — all ~195 commands by category with known gaps listed |
| [scripts/README.md](/c:/Python/Duke/Standalone/scripts/README.md) | Index of reusable scripts and helper categories |
| [docs](/c:/Python/Duke/Standalone/docs) | Durable operator docs, architecture, workflow guides |
| [docs/reports](/c:/Python/Duke/Standalone/docs/reports) | Dated evidence, investigations, and validation outputs |
| [app/](/c:/Python/Duke/Standalone/app) | Streamlit UI apps |
| [data/db/duke_rates.db](/c:/Python/Duke/Standalone/data/db/duke_rates.db) | Primary SQLite database |
| [.mcp.json](/c:/Python/Duke/Standalone/.mcp.json) | GitNexus MCP server config (code graph, impact analysis, Cypher queries) |
| [docs/gitnexus_usage_guide.md](/c:/Python/Duke/Standalone/docs/gitnexus_usage_guide.md) | How to use GitNexus for navigation, impact analysis, and the four common questions |

## Documentation Model

Use the docs in layers instead of trying to store everything in one file.

| Layer | Location | Use It For | Update When |
|---|---|---|---|
| Entry point | `AGENT_ONBOARDING.md` | Read order, repo-wide rules, top-level routing | onboarding path or repo-wide rules changed |
| Tool-use policy | `docs/agent_tool_use_policy.md` | default tool-choice rules, promotion rules, anti-patterns | agent tool/workflow behavior or policy changed |
| Machine-readable manifests | `docs/agent_tool_registry.json`, `docs/agent_workflows.json` | supported tool selection, default workflow chaining, legacy avoidance | supported tool/workflow surface changed |
| Canonical workflow/system docs | `docs/*.md` without dates | durable workflows, architecture, source-of-truth rules | behavior or workflow changed |
| Command reference | `docs/cli_command_reference.md` | full command surface, flags, gaps | new commands added or gaps closed |
| Tool index | `scripts/README.md` | finding existing helpers and script categories | adding or changing reusable scripts |
| Current-state handoff | `docs/NEXT_SESSION_START_HERE.md`, `docs/NEXT_SESSION_PRIORITIES.md` | short operational handoff and next actions | priorities or operational status changed |
| Evidence and investigations | `docs/reports/*.md` | dated findings, experiments, validation, audits | session-specific output worth keeping |
| External memory | outside-repo memory files or raw LLM summaries | temporary context only | promote durable findings into repo docs before relying on them |

If a durable lesson lives only in a dated report or an external memory file, the documentation system has failed. Promote it into a canonical doc.

## Start By Task

### Historical pipeline operation

Read:
- [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)
- [ncuc_pipeline_overview.md](/c:/Python/Duke/Standalone/docs/ncuc_pipeline_overview.md)

### Parser, profile, OCR, or targeted reprocessing work

Read:
- [agent_task_routing.md](/c:/Python/Duke/Standalone/docs/agent_task_routing.md)
- [historical_parser_architecture.md](/c:/Python/Duke/Standalone/docs/historical_parser_architecture.md)
- [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)

### Schema, persistence, lineage, or provenance work

Read:
- [architecture.md](/c:/Python/Duke/Standalone/docs/architecture.md)
- [source_of_truth_and_legacy_paths.md](/c:/Python/Duke/Standalone/docs/source_of_truth_and_legacy_paths.md)
- [technical_debt.md](/c:/Python/Duke/Standalone/docs/technical_debt.md)

### Missing clean-document recovery

Read:
- [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md)
- [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)
- [cli_command_reference.md](/c:/Python/Duke/Standalone/docs/cli_command_reference.md)

Use:
- `workflow search-nc-missing-clean-docs`
- `workflow run-nc-missing-doc`
- `workflow report-nc-missing-doc-triage`
- `workflow execute-top-nc-missing-doc-triage`
- `workflow execute-batch-nc-missing-doc-triage`
- `workflow show-nc-missing-doc-status`
- `workflow report-nc-missing-doc-deferred`
- `workflow remediate-and-promote-nc-missing-docs`

Preferred weak-agent loop:
1. `workflow run-nc-missing-doc`
2. `workflow report-nc-missing-doc-triage --actionable-only --top 10`
3. `workflow execute-top-nc-missing-doc-triage` for one bounded step
4. `workflow execute-batch-nc-missing-doc-triage --max-actions N` only when bounded multi-step progress is appropriate

Search behavior note:
- difficult missing-document searches now broaden automatically from exact
  docket search to nearby docket variants, docketless broad structured search,
  and richer keyword fan-out using schedule/title/leaf/redline clues
- docket-id lookup can now return normalized or near matches instead of only
  exact visible-text labels
- when direct portal search is weak, prefer the triage queue and remediation
  commands instead of inventing ad hoc docket expansions

### Existing tools and helper discovery

Read in order — use the manifests first, fall back to the broader reference only if needed:

1. [agent_tool_registry.json](/c:/Python/Duke/Standalone/docs/agent_tool_registry.json) — which tools are supported vs. legacy vs. compatibility aliases
2. [agent_workflows.json](/c:/Python/Duke/Standalone/docs/agent_workflows.json) — sanctioned workflow chains with step-by-step tool sequences
3. [agent_tool_use_policy.md](/c:/Python/Duke/Standalone/docs/agent_tool_use_policy.md) — human-readable rules for promotion, reuse, and anti-pattern avoidance
4. [cli_command_reference.md](/c:/Python/Duke/Standalone/docs/cli_command_reference.md) — full command surface when the manifests don't cover the needed command
5. [scripts/README.md](/c:/Python/Duke/Standalone/scripts/README.md) — scripts not yet promoted to CLI
6. [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md) — narrative workflow descriptions and anti-patterns

### Current operational status

Read:
- [NEXT_SESSION_START_HERE.md](/c:/Python/Duke/Standalone/docs/NEXT_SESSION_START_HERE.md)
- [NEXT_SESSION_PRIORITIES.md](/c:/Python/Duke/Standalone/docs/NEXT_SESSION_PRIORITIES.md)
- latest dated summary only if needed for immediate context

## Critical Rules

- Treat `data/db/duke_rates.db` as the active database unless the task explicitly says otherwise.
- Prefer database-backed pipeline state over session memory or stale exports.
- Prefer targeted queues and selective reprocessing over broad reruns.
- Prefer supported tools from `agent_tool_registry.json` over legacy or compatibility-alias tools.
- Prefer sanctioned workflow chains from `agent_workflows.json` over ad hoc command sequences.
- Prefer canonical docs over dated reports when both exist.
- Preserve provenance, diagnostics, and review state when changing parsing or reprocessing logic.
- For NCUC portal automation, use installed Chrome or Edge rather than bundled Playwright Chromium.
- Keep reusable helpers under `scripts/` or promote recurring operator workflows into the CLI.
- Keep new durable docs under `docs/`; keep new dated investigations under `docs/reports/`; avoid new root-level session markdown.
- Do not treat `mine-ncuc-pipeline` as the primary intake command — it is a compatibility alias for `ncuc import-pipeline`. Do not run both simultaneously.

## Knowledge Preservation Rules

- If you changed a workflow, update the canonical workflow doc in the same task.
- If you improved the sanctioned path for a recurring task, update [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md) and [agent_workflows.json](/c:/Python/Duke/Standalone/docs/agent_workflows.json).
- If the improvement changes how agents should choose or promote tools, update [agent_tool_use_policy.md](/c:/Python/Duke/Standalone/docs/agent_tool_use_policy.md).
- If you added a reusable helper, update [scripts/README.md](/c:/Python/Duke/Standalone/scripts/README.md).
- If a command moved from gap to implemented, update [cli_command_reference.md](/c:/Python/Duke/Standalone/docs/cli_command_reference.md) and [agent_tool_registry.json](/c:/Python/Duke/Standalone/docs/agent_tool_registry.json).
- If you discovered a durable trap, constraint, or operating rule, promote it into an undated doc instead of leaving it only in a dated report.
- If you generated session findings, keep them short and evidence-oriented; move only reusable lessons into canonical docs.
- Keep this file small. Add links and routing here, not long implementation narratives.

See [knowledge_capture_workflow.md](/c:/Python/Duke/Standalone/docs/knowledge_capture_workflow.md) for the full rule set.

## Quick Commands

```powershell
python -m duke_rates --help
python -m duke_rates show-workflow-status-nc
python -m duke_rates lineage show-gaps-nc
python -m duke_rates lineage show-fingerprint-coverage-nc
python -m duke_rates ncuc import-pipeline --all-downloaded
python -m duke_rates bootstrap-missing-versions-nc
python -m duke_rates extract-rates-nc
python -m duke_rates parse-review-summary
```

## When You Are Stuck

- Need system context: [architecture.md](/c:/Python/Duke/Standalone/docs/architecture.md)
- Need pipeline usage: [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)
- Need sanctioned default paths: [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md) or [agent_workflows.json](/c:/Python/Duke/Standalone/docs/agent_workflows.json)
- Need tool discovery: [agent_tool_registry.json](/c:/Python/Duke/Standalone/docs/agent_tool_registry.json) or [cli_command_reference.md](/c:/Python/Duke/Standalone/docs/cli_command_reference.md)
- Need current priorities: [NEXT_SESSION_PRIORITIES.md](/c:/Python/Duke/Standalone/docs/NEXT_SESSION_PRIORITIES.md)
- Need handoff rules: [knowledge_capture_workflow.md](/c:/Python/Duke/Standalone/docs/knowledge_capture_workflow.md)

**Status:** Active
**Review Trigger:** Update this file only when the onboarding path or repo-wide rules change
