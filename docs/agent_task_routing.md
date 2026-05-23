# Agent Task Routing

Use this document to decide which docs, commands, and code areas matter for a
specific task. The goal is to keep a new agent from scanning large parts of the
repo unnecessarily.

## Start Here

Always read these first:

1. [README.md](/c:/Python/Duke/Standalone/README.md)
2. [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)
3. [source_of_truth_and_legacy_paths.md](/c:/Python/Duke/Standalone/docs/source_of_truth_and_legacy_paths.md)
4. [agent_tool_use_policy.md](/c:/Python/Duke/Standalone/docs/agent_tool_use_policy.md)
5. [agent_workflows.json](/c:/Python/Duke/Standalone/docs/agent_workflows.json)
6. [agent_tool_registry.json](/c:/Python/Duke/Standalone/docs/agent_tool_registry.json)

Use the JSON manifests first for the supported/default path. Use the CLI reference only when you need the broader command surface:
- [cli_command_reference.md](/c:/Python/Duke/Standalone/docs/cli_command_reference.md)

## Task Routing Matrix

### Historical pipeline operation

Read:
- [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)
- [ncuc_pipeline_overview.md](/c:/Python/Duke/Standalone/docs/ncuc_pipeline_overview.md)

Use:
- `ncuc-import-pipeline`
- `mine-ncuc-pipeline` (compatibility alias; prefer `ncuc-import-pipeline`)
- `bootstrap-missing-versions-nc`
- `extract-rates-nc`
- `validate-extraction-nc`

### Parser-profile work

Read:
- [historical_parser_architecture.md](/c:/Python/Duke/Standalone/docs/historical_parser_architecture.md)
- [ncuc_pipeline_overview.md](/c:/Python/Duke/Standalone/docs/ncuc_pipeline_overview.md)

Touch:
- [parser_profiles.py](/c:/Python/Duke/Standalone/src/duke_rates/historical/ncuc/pipeline/parser_profiles.py)
- [bulk_extractor.py](/c:/Python/Duke/Standalone/src/duke_rates/historical/ncuc/pipeline/bulk_extractor.py)
- related tests in [test_historical_parser_profiles.py](/c:/Python/Duke/Standalone/tests/test_historical_parser_profiles.py)

### OCR and scanned-document work

Read:
- [OCR_IMPLEMENTATION_PLAN.md](/c:/Python/Duke/Standalone/docs/OCR_IMPLEMENTATION_PLAN.md)
- [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)

Touch:
- [ocr.py](/c:/Python/Duke/Standalone/src/duke_rates/historical/ncuc/pipeline/ocr.py)
- [ocr_queue.py](/c:/Python/Duke/Standalone/src/duke_rates/db/ocr_queue.py)
- [triage.py](/c:/Python/Duke/Standalone/src/duke_rates/historical/ncuc/pipeline/triage.py)

Use:
- `ocr enqueue-nc`
- `ocr show-queue-nc`
- `ocr process-queue-nc`

### Weak-parse or targeted reprocessing work

Read:
- [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)
- [ncuc_pipeline_overview.md](/c:/Python/Duke/Standalone/docs/ncuc_pipeline_overview.md)

Touch:
- [reprocess.py](/c:/Python/Duke/Standalone/src/duke_rates/db/reprocess.py)
- [stage_versions.py](/c:/Python/Duke/Standalone/src/duke_rates/historical/ncuc/pipeline/stage_versions.py)
- [profile_dependencies.py](/c:/Python/Duke/Standalone/src/duke_rates/historical/ncuc/pipeline/profile_dependencies.py)

Use:
- `parse-review-summary`
- `reprocess enqueue-nc`
- `reprocess show-stale-historical-nc`
- `reprocess show-profile-impact-nc`

### Lineage or family-link audit

Read:
- [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md)
- [source_of_truth_and_legacy_paths.md](/c:/Python/Duke/Standalone/docs/source_of_truth_and_legacy_paths.md)

Touch:
- [lineage_gaps.py](/c:/Python/Duke/Standalone/src/duke_rates/historical/ncuc/lineage_gaps.py)
- [family_mismatch_audit.py](/c:/Python/Duke/Standalone/src/duke_rates/historical/family_mismatch_audit.py)

Use:
- `lineage show-gaps-nc`
- `lineage validate-nc`
- `lineage suggest-family-links-nc`
- `scripts/maintenance/audit_historical_family_mismatches.py`

### Missing clean-document recovery

Read:
- [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md)
- [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)
- [cli_command_reference.md](/c:/Python/Duke/Standalone/docs/cli_command_reference.md)

Touch:
- [missing_doc_workflow.py](/c:/Python/Duke/Standalone/src/duke_rates/historical/ncuc/missing_doc_workflow.py)
- [missing_clean_doc_search.py](/c:/Python/Duke/Standalone/src/duke_rates/historical/ncuc/missing_clean_doc_search.py)
- [missing_doc_remediation.py](/c:/Python/Duke/Standalone/src/duke_rates/historical/ncuc/missing_doc_remediation.py)

Use:
- `workflow search-nc-missing-clean-docs`
- `workflow run-nc-missing-doc`
- `workflow show-nc-missing-doc-status`
- `workflow report-nc-missing-doc-deferred`
- `workflow remediate-and-promote-nc-missing-docs`

### Provenance or fingerprint coverage audit

Read:
- [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md)
- [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)

Touch:
- [provenance_gaps.py](/c:/Python/Duke/Standalone/src/duke_rates/historical/ncuc/provenance_gaps.py)
- [fingerprint_coverage.py](/c:/Python/Duke/Standalone/src/duke_rates/historical/ncuc/fingerprint_coverage.py)

Use:
- `lineage show-provenance-gaps-nc`
- `lineage show-fingerprint-coverage-nc`
- `lineage validate-nc`

### Data model / schema work

Read:
- [architecture.md](/c:/Python/Duke/Standalone/docs/architecture.md)
- [technical_debt.md](/c:/Python/Duke/Standalone/docs/technical_debt.md)

Touch:
- [schema.py](/c:/Python/Duke/Standalone/src/duke_rates/db/schema.py)
- [pipeline.py](/c:/Python/Duke/Standalone/src/duke_rates/models/pipeline.py)

### Repo hygiene / helper placement / GitHub prep

Read:
- [repository_hygiene.md](/c:/Python/Duke/Standalone/docs/repository_hygiene.md)
- [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)

### Roadmap or implementation planning

Read:
- [roadmap.md](/c:/Python/Duke/Standalone/docs/roadmap.md)
- [ncuc_pipeline_overview.md](/c:/Python/Duke/Standalone/docs/ncuc_pipeline_overview.md)

Rule:
- if work moved from planned to implemented, update the roadmap in the same task
- if a workflow changed, update the operator docs in the same task

## If The Task Is Unclear

Use this decision rule:

- operating the historical pipeline -> start with the pipeline guide
- changing parser behavior -> start with parser architecture + related tests
- rerunning data selectively -> start with review/reprocess docs and queue commands
- recovering missing clean documents -> start with operator workflows + missing-doc commands
- auditing provenance or fingerprint coverage -> start with the provenance/fingerprint audit commands
- changing schema or persistence -> start with architecture + technical debt
- creating a helper -> start with repository hygiene + helper policy
