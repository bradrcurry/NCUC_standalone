# Agent Tool Use Policy

Use this doc to decide how another AI agent should use local tools and when a workflow/tool gap should trigger improvement work instead of a one-off workaround.

## Core Rule

Prefer improving the shared local tool or documented workflow over creating a bespoke script for a recurring task.

## Preferred Order

Use tools in this order:

1. Supported CLI commands in [agent_tool_registry.json](/c:/Python/Duke/Standalone/docs/agent_tool_registry.json)
2. Sanctioned workflow chains in [agent_workflows.json](/c:/Python/Duke/Standalone/docs/agent_workflows.json)
3. Narrative workflow guidance in [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md)
4. Full command lookup in [cli_command_reference.md](/c:/Python/Duke/Standalone/docs/cli_command_reference.md)
5. Reusable helpers in [scripts/README.md](/c:/Python/Duke/Standalone/scripts/README.md)
6. Narrow `scripts/debug/` helpers only when the supported surfaces still do not answer the question

## Rules For Agents

- Prefer local tools over manual database inspection.
- Prefer parameterized commands over hard-coded scripts.
- Prefer targeted queues and bounded reprocessing over broad reruns.
- Prefer sanctioned workflows over command improvisation when the task already has a documented path.
- Treat JSON manifests as the default supported source of truth for tools and workflow routing.
- Treat dated reports as evidence and context, not the default operating surface.

## When A Gap Is Real

A tooling or workflow gap is real when one of these is true:

- the same manual inspection pattern appears in more than one meaningful task
- a repeated SQL query is the only practical way to answer an operator question
- another agent would likely have to rediscover the same command sequence
- a sanctioned workflow is missing a key step, is misleading, or is too expensive
- a helper exists but is still too hard-coded to be safely reused

## What To Do When A Gap Is Found

1. Confirm the existing CLI, workflow docs, and reusable scripts do not already solve it.
2. Improve the nearest shared tool or workflow when feasible.
3. If a temporary helper is still needed, place it under the correct `scripts/` folder.
4. Document where it fits and whether it is temporary or reusable.
5. Record the improvement candidate in [tool_workflow_backlog.md](/c:/Python/Duke/Standalone/docs/tool_workflow_backlog.md) if it is not fully resolved in the same task.

## Promotion Rules

Promote a helper into a stronger shared surface when:

- multiple agents are likely to need it
- it answers a recurring inspection or repair question
- it can be parameterized safely
- it belongs in routine intake, extraction, review, OCR, lineage, or reprocess work
- leaving it as a one-off would force workflow rediscovery

Preferred promotion targets:

- recurring operator task -> CLI command
- reusable maintenance/repair helper -> `scripts/maintenance/`
- reusable export/report helper -> `scripts/exports/`
- temporary investigation aid -> `scripts/debug/`
- intake/backfill runner -> `scripts/ingestion/`

## Anti-Patterns

Avoid:

- creating a new root-level helper for normal workflow use
- writing a one-off script when an existing CLI command only needs a small improvement
- documenting a new workflow only in a chat summary
- adding another overlapping handoff doc instead of updating the canonical doc
- treating compatibility aliases or legacy tools as the default path for new work

## Documentation Requirements

When tool or workflow behavior changes, update the matching docs in the same task:

- supported tool surface changed -> [agent_tool_registry.json](/c:/Python/Duke/Standalone/docs/agent_tool_registry.json)
- sanctioned workflow changed -> [agent_workflows.json](/c:/Python/Duke/Standalone/docs/agent_workflows.json) and [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md)
- command details changed -> [cli_command_reference.md](/c:/Python/Duke/Standalone/docs/cli_command_reference.md)
- helper usage changed -> [scripts/README.md](/c:/Python/Duke/Standalone/scripts/README.md)
- onboarding path changed -> [AGENT_ONBOARDING.md](/c:/Python/Duke/Standalone/AGENT_ONBOARDING.md)

## Default Reminder

If you are about to build a bespoke tool, ask:

- is there already a sanctioned workflow for this?
- is the real problem a missing filter, summary, or queue view in an existing command?
- will another agent need this again?

If the answer to the last two is yes, improve the shared tool/workflow.
