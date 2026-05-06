# Knowledge Capture Workflow

This doc defines how project knowledge should be preserved so future agents do not need to rediscover the same workflows, traps, and operating rules.

Use this doc when deciding:

- where new knowledge belongs
- what should be promoted out of session notes
- which document should be updated when code or workflow changes
- how to keep onboarding small without losing critical context

## Principles

- One short onboarding router is better than one giant onboarding encyclopedia.
- Durable workflow knowledge belongs in canonical docs, not in dated reports.
- Dated reports should preserve evidence and decisions, not become the only copy of operational knowledge.
- Database-backed state and code are authoritative for current behavior; docs should explain them, not contradict them.
- If a future agent would likely need a fact again, it should not remain only in chat, memory, or an external summary file.

## Documentation Tiers

### Tier 1: Entry Point

Use [AGENT_ONBOARDING.md](/c:/Python/Duke/Standalone/AGENT_ONBOARDING.md) for:

- first-read order
- repo-wide rules
- shortest path into the active workflow docs

Keep it small. Add links, not long command catalogs or session history.

### Tier 2: Canonical Docs

Use undated docs in [docs](/c:/Python/Duke/Standalone/docs) for durable knowledge:

- architecture and schema expectations
- source-of-truth rules
- operator workflows
- tool-use policy and promotion rules
- task-routing guidance
- reusable troubleshooting guidance

Examples:

- [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)
- [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md)
- [agent_tool_use_policy.md](/c:/Python/Duke/Standalone/docs/agent_tool_use_policy.md)
- [source_of_truth_and_legacy_paths.md](/c:/Python/Duke/Standalone/docs/source_of_truth_and_legacy_paths.md)
- [agent_task_routing.md](/c:/Python/Duke/Standalone/docs/agent_task_routing.md)
- [architecture.md](/c:/Python/Duke/Standalone/docs/architecture.md)
- [historical_parser_architecture.md](/c:/Python/Duke/Standalone/docs/historical_parser_architecture.md)

### Tier 3: Current-State Handoff

Use short current-state docs for what is true right now but may change soon:

- [NEXT_SESSION_START_HERE.md](/c:/Python/Duke/Standalone/docs/NEXT_SESSION_START_HERE.md)
- [NEXT_SESSION_PRIORITIES.md](/c:/Python/Duke/Standalone/docs/NEXT_SESSION_PRIORITIES.md)

These should stay concise:

- what just changed
- what remains open
- what the next agent should do first

Do not move deep architecture or workflow rules into these files.

### Tier 4: Reports and Evidence

Use dated files under [docs/reports](/c:/Python/Duke/Standalone/docs/reports) for:

- investigations
- validations
- extraction or audit results
- experimental runs
- supporting evidence for decisions

These are important, but they are not the primary onboarding path.

### Tier 5: External Memory and Raw Session Summaries

Examples:

- local memory files outside the repo
- LLM-generated summaries
- ad hoc session notes
- root-level dated markdown created during active work

These are temporary context sources. If they contain durable knowledge, promote that knowledge into Tier 2 or Tier 3 docs.

## Update Matrix

When a change happens, update the matching doc in the same task.

| Change Type | Primary Doc To Update | Secondary Doc To Update |
|---|---|---|
| onboarding path changed | [AGENT_ONBOARDING.md](/c:/Python/Duke/Standalone/AGENT_ONBOARDING.md) | this doc |
| sanctioned default workflow changed | [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md) | relevant domain workflow doc |
| tool-choice or promotion rule changed | [agent_tool_use_policy.md](/c:/Python/Duke/Standalone/docs/agent_tool_use_policy.md) | [agent_task_routing.md](/c:/Python/Duke/Standalone/docs/agent_task_routing.md) |
| historical workflow or command changed | [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md) | [agent_task_routing.md](/c:/Python/Duke/Standalone/docs/agent_task_routing.md) |
| parser-profile or OCR behavior changed | [historical_parser_architecture.md](/c:/Python/Duke/Standalone/docs/historical_parser_architecture.md) | pipeline guide |
| schema, provenance, lineage, or source-of-truth changed | [architecture.md](/c:/Python/Duke/Standalone/docs/architecture.md) | [source_of_truth_and_legacy_paths.md](/c:/Python/Duke/Standalone/docs/source_of_truth_and_legacy_paths.md) |
| reusable helper added or changed | [scripts/README.md](/c:/Python/Duke/Standalone/scripts/README.md) | relevant workflow doc |
| current priorities changed | [NEXT_SESSION_PRIORITIES.md](/c:/Python/Duke/Standalone/docs/NEXT_SESSION_PRIORITIES.md) | [NEXT_SESSION_START_HERE.md](/c:/Python/Duke/Standalone/docs/NEXT_SESSION_START_HERE.md) |
| one-off evidence worth keeping | dated report in [docs/reports](/c:/Python/Duke/Standalone/docs/reports) | promote durable lessons into canonical docs |

## Promotion Rules

Promote a fact into a canonical doc if any of these are true:

- it changes how future work should be performed
- it explains why a known approach fails or succeeds
- it affects where source-of-truth data should be read from
- it affects how parsing, lineage, OCR, review, or reprocessing should be done
- it is likely to be needed again outside the current session

Keep a fact only in a report if it is mainly:

- evidence of a specific run
- a dated audit snapshot
- a reversible experiment
- a temporary measurement that does not change workflow

## Handoff Minimum

Before closing substantial work:

1. Update the canonical doc for any workflow or behavior change.
2. Update [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md) if the sanctioned default path changed.
3. Update [scripts/README.md](/c:/Python/Duke/Standalone/scripts/README.md) for reusable helpers.
4. Update current-state docs if priorities or next actions changed.
5. Add a dated report only if the evidence itself is worth preserving.
6. Do not leave critical operating knowledge only in chat or an external memory file.

## Anti-Patterns

Avoid:

- expanding onboarding until it duplicates half of `docs/`
- storing the only copy of a workflow in a dated session summary
- leaving reusable commands undocumented after adding a helper
- creating new root-level markdown for normal session handoff
- copying the same command list into many docs and letting them drift

## Practical Rule For This Repo

If another AI agent needs a single pointer, give them:

1. [AGENT_ONBOARDING.md](/c:/Python/Duke/Standalone/AGENT_ONBOARDING.md)
2. [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md)
3. [agent_tool_use_policy.md](/c:/Python/Duke/Standalone/docs/agent_tool_use_policy.md)
4. [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)
5. [agent_task_routing.md](/c:/Python/Duke/Standalone/docs/agent_task_routing.md)

That is the compact onboarding path. Everything else should branch from there.
