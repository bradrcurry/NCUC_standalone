# Agent Change Checklist

Use this checklist before closing work so future agents inherit a clean and
consistent repo state.

## Before You Start

- read [AGENT_ONBOARDING.md](/c:/Python/Duke/Standalone/AGENT_ONBOARDING.md)
- read the task-specific docs from [agent_task_routing.md](/c:/Python/Duke/Standalone/docs/agent_task_routing.md)
- confirm whether the task should use targeted queues instead of broad reruns

## While You Work

- keep reusable logic in `src/`
- keep reusable helpers in the correct `scripts/` subfolder
- avoid leaving ad hoc files in the repo root
- prefer parameterized CLI or documented helpers for repeated workflows
- preserve provenance, diagnostics, and review state where relevant

## Before You Finish

- add or update tests when behavior changed
- update operator docs if commands, workflows, or helper expectations changed
- update [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md) if the sanctioned default path changed
- apply the promotion rules in [knowledge_capture_workflow.md](/c:/Python/Duke/Standalone/docs/knowledge_capture_workflow.md)
- update [roadmap.md](/c:/Python/Duke/Standalone/docs/roadmap.md) if planned work became implemented or materially changed priority
- document any reusable helper you created
- document any new guardrail another agent should know
- avoid leaving critical context only in chat/session notes

## If You Created A New Helper

- place it under `scripts/exports/`, `scripts/maintenance/`, `scripts/debug/`, or `scripts/ingestion/`
- use a descriptive filename
- make its purpose and scope clear
- promote it into the CLI if it answers a recurring operator need
- update [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md) if it is now part of the recommended workflow

## If You Changed Parsing Or Reprocessing Logic

- update the relevant architecture or pipeline doc
- update tests for parser selection, fallback, or queue behavior as needed
- prefer selective reparsing paths over full archive reruns
