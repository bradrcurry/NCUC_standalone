# Source Of Truth And Legacy Paths

This doc exists to stop agents from treating every historical workflow in the
repo as equally current.

## Source Of Truth Principles

For authoritative tariff facts:

- deterministic parsing and DB-backed persisted results are the source of truth
- review outcomes, parse attempts, and processing runs are durable operational
  state
- cached OCR/page/span artifacts are reusable intermediate state, not the final
  truth layer
- LLM-assisted analysis is planned as an assistive layer, not the authority for
  tariff facts

## Current Preferred Historical Path

For NCUC historical document work, the preferred path is:

1. discovery/download
2. page-aware mining and segmentation
3. family matching
4. `historical_documents` / `tariff_versions`
5. parser-profile extraction
6. review, OCR, and targeted reprocessing queues

Primary docs:
- [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)
- [ncuc_pipeline_overview.md](/c:/Python/Duke/Standalone/docs/ncuc_pipeline_overview.md)

## Current Operational Tables

The main operational state for the historical path includes:

- `historical_documents`
- `tariff_versions`
- `document_fingerprints`
- `parse_attempt_logs`
- `parse_review_outcomes`
- `historical_processing_runs`
- `historical_reprocess_queue`
- `ocr_processing_queue`
- `ocr_artifacts`
- `ncuc_page_artifacts`
- `ncuc_span_artifacts`

If an agent needs to understand what the pipeline currently knows, these tables
matter more than old session transcripts or ad hoc JSON snapshots.

## Compatibility And Legacy Paths

These still exist, but should not be treated as the default historical path:

- older `ingest-ncuc` JSON export flow
- `load-ncuc-ingest` JSON-to-DB handoff
- ad hoc scripts under `scripts/debug/`
- stale investigation notes in `docs/reports/` when newer operator docs exist

These are still useful in some cases:

- JSON exports for audit/debug
- debug scripts for narrow inspection when the CLI lacks a needed view
- older reports for historical context

But they are not the primary operating model.

## What To Prefer

Prefer:

- CLI commands over one-off scripts
- targeted queues over broad reruns
- DB-backed diagnostic state over session memory
- current docs over old reports
- reusable helpers over one-off repo-root files

## What To Avoid

Avoid:

- assuming every parsing path in the repo is equally current
- starting with broad rescans
- using legacy JSON artifacts as if they were the main system of record
- leaving important workflow knowledge only in an agent transcript
