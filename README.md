# Duke Rates

`duke-rates` is a Python platform for discovering, archiving, parsing, validating, and analyzing Duke Energy tariff documents. The most mature part of the project is now North Carolina residential history and bill reconstruction for:

- DEP: Duke Energy Progress `RES`
- DEC: Duke Energy Carolinas `RS`

The project can now answer the practical question:

> What did this customer likely pay on a given date, which riders were in effect, how much of the bill came from base vs riders, and how confident are we in that reconstruction?

## Current State

### What is in good shape

- SQLite-backed historical dataset in [data/db/duke_rates.db](c:/Python/Duke/Standalone/data/db/duke_rates.db)
- DEP `RES` residential base-rate history covering the usable `2016–2026` timeline
- DEP rider history split into:
  - clean rider-summary history from `Leaf 600`
  - provisional pre-`Leaf 600` history persisted in dedicated DB tables
- DEC `RS` residential base and rider history with carry-forward coverage for the current comparison timeline
- Canonical residential timeline export for DEP vs DEC
- Streamlit + Plotly analytical apps on top of the canonical timeline
- Duke bill parsing and bill-to-engine reconciliation against actual bills
- XML interval usage ingestion from Duke exports
- TOU parsing for DEP `R-TOU-CPP`
- Storm rider handling including stacked `STS` / `STS-2` behavior and mid-bill proration

### What has been validated recently

- Actual bill reconciliation for `12` saved Duke bills now lands at `12/12 good_match`
- Max absolute bill delta in the saved-bill validation set is now `$0.45`
- Summary rider effective dates are supported by bill splits at:
  - `2025-04-01`
  - `2025-12-01`
  - `2026-01-01`
- Storm rider behavior is materially improved and works in the current reconciliation path
- DEP `RES` component-level rider dates are mostly populated:
  - `97.0%` overall component-date completeness
  - `99.0%` if you exclude undated aggregate `BA` subtotal rows
- The Historical NCUC text mining pipeline was successfully refactored (2026-03-25) into a page-aware, staged segmentation architecture resolving multi-leaf compliance book ambiguity.

## Core Data Model

The project currently relies on a practical hybrid model rather than the originally planned fully generalized tariff-version schema.

### Main SQLite tables in active use

- `ncuc_ingest_segments`
  - parsed base-rate schedule segments
  - used for DEP `RES` and DEC `RS` base-rate history
- `rider_summary_blocks`
  - normalized rider-summary blocks such as DEP `Leaf 600` and DEC `Leaf 99`
- `rider_line_items`
  - rider components inside each summary block, including component-level effective dates
- `dep_provisional_rider_totals`
  - persisted provisional DEP rider totals for older periods without clean summary-sheet coverage
- `dep_provisional_rider_components`
  - persisted provisional DEP component rows
- `bill_statements`
  - parsed actual Duke bills
- `bill_component_observations`
  - normalized line-item observations derived from actual bills
- `documents` / `historical_documents`
  - current and historical source document catalog

### Canonical residential timeline

The most important derived dataset for analysis is the canonical residential timeline:

- [canonical_residential_timeline.csv](c:/Python/Duke/Standalone/data/processed/canonical_residential/canonical_residential_timeline.csv)

It standardizes the comparison schema for DEP `RES` and DEC `RS` with fields such as:

- `utility`
- `schedule`
- `effective_date`
- `rider_effective_date`
- `base_cents_per_kwh`
- `rider_cents_per_kwh`
- `all_in_cents_per_kwh`
- `base_bill_amount`
- `all_in_bill_amount`
- `fixed_monthly_charge`
- `rider_coverage_status`
- `bill_coverage_status`
- `rider_source_kind`
- `rider_quality_flag`

## Repository Layout

```text
src/duke_rates/
  analytics/        History exports, validation, audits, canonical timeline
  billing/          Billing engine, riders, TOU logic, reconciliation
  db/               Schema, repository, loaders, cleanup utilities
  historical/       Recovery, NCUC portal/search workflows, lineage
  models/           Pydantic models
  parse/            PDF/HTML/bill/rider/schedule parsers
  charts/           Plotly chart layer
  cli.py            CLI entrypoint
  config.py         Environment settings

data/
  db/               SQLite database
  raw/              Current Duke-hosted documents
  historical/       Historical NCUC and archived documents
  processed/        CSV/JSON exports, validation reports, app inputs
  usage/            Duke interval XML exports

docs/
  document_parsing_pipeline_guide.md
  docling_integration_plan.md
  json_artifact_inventory.md
  OCR_IMPLEMENTATION_PLAN.md
  architecture.md
  known_issues.md
  repository_hygiene.md
  roadmap.md
  reports/            Durable investigation and validation reports

scripts/
  exports/          Reusable export runners
  maintenance/      Repair, verification, and cleanup helpers
  debug/            Local inspection and debugging helpers

tests/
  Regression and parser/billing tests
```

## Quick Start

### 1. Create and activate a virtual environment

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 2. Install

Base plus dev tools:

```powershell
python -m pip install -e .[dev]
```

Useful extras for this repo:

```powershell
python -m pip install -e .[dev,browser,pdf,ocr,viz]
```

If you plan to use the OCR queue or OCR-required document path, you also need
the system Tesseract binary installed and available on `PATH`.

If you want Playwright support:

```powershell
playwright install chromium
```

### 3. Configure `.env`

```powershell
Copy-Item .env.example .env
```

Important optional settings:

| Variable | Purpose |
|---|---|
| `DUKE_RATES_NCID_USERNAME` | NCID login for authenticated NCUC portal access |
| `DUKE_RATES_NCID_PASSWORD` | NCID login for authenticated NCUC portal access |
| `DUKE_RATES_OPENEI_API_KEY` | Optional OpenEI lookups |
| `DUKE_RATES_GOOGLE_API_KEY` | Optional Google CSE access |
| `DUKE_RATES_GOOGLE_CSE_ID` | Optional Google CSE engine ID |

### 4. CLI entrypoint

```powershell
python -m duke_rates --help
```

Or, if installed as a script:

```powershell
duke-rates --help
```

For a browsable command map, use:

- [cli_command_reference.md](/c:/Python/Duke/Standalone/docs/cli_command_reference.md)
- [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md)
- [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)

## Most Useful Workflows Right Now

If you are an AI agent or setting up work for multiple AI agents, start with:

- [AGENT_ONBOARDING.md](/c:/Python/Duke/Standalone/AGENT_ONBOARDING.md)
- [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md)
- [agent_task_routing.md](/c:/Python/Duke/Standalone/docs/agent_task_routing.md)
- [source_of_truth_and_legacy_paths.md](/c:/Python/Duke/Standalone/docs/source_of_truth_and_legacy_paths.md)

### Historical document parsing pipeline

If you are working on the NCUC historical parsing pipeline, start with:

- [AGENT_ONBOARDING.md](/c:/Python/Duke/Standalone/AGENT_ONBOARDING.md)
- [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md)
- [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)
- [ncuc_pipeline_overview.md](/c:/Python/Duke/Standalone/docs/ncuc_pipeline_overview.md)
- [historical_parser_architecture.md](/c:/Python/Duke/Standalone/docs/historical_parser_architecture.md)
- [docling_integration_plan.md](/c:/Python/Duke/Standalone/docs/docling_integration_plan.md)

Those docs are the intended shortest path for operators and AI agents. They are
meant to avoid broad repo scans just to understand how to run the pipeline.
They also define the policy for agent-created helpers, including when a bespoke
script should be stored under `scripts/` versus promoted into the CLI. If the
task involves heavier OCR/layout/table analysis or GPU evaluation, use the
Docling plan alongside the OCR plan rather than inventing a parallel workflow.

### CLI Quick Reference

For normal NC historical work, these command groups are the current default
surface:

Session orientation:

```powershell
python -m duke_rates show-workflow-status-nc
python -m duke_rates parse-review-summary
python -m duke_rates reprocess show-queue-nc
python -m duke_rates reprocess show-stale-historical-nc
python -m duke_rates ocr show-queue-nc
```

Discovery, intake, and extraction:

```powershell
python -m duke_rates ncuc-smart-search --family-key nc-progress-leaf-602
python -m duke_rates ncuc-fetch-portal --limit 50
python -m duke_rates ncuc-import-pipeline --all-downloaded
python -m duke_rates bootstrap-missing-versions-nc
python -m duke_rates extract-rates-nc
```

Manual registration and targeted repair:

```powershell
python -m duke_rates lineage add-historical-document-nc --help
python -m duke_rates lineage rebind-historical-page-range --help
python -m duke_rates reprocess enqueue-nc --hd-id <historical_document_id>
python -m duke_rates reprocess process-queue-nc
```

Lineage, provenance, and fingerprint audit:

```powershell
python -m duke_rates lineage show-gaps-nc
python -m duke_rates lineage validate-nc
python -m duke_rates lineage show-provenance-gaps-nc
python -m duke_rates lineage show-fingerprint-coverage-nc
```

Missing-document recovery loop:

```powershell
python -m duke_rates workflow search-nc-missing-clean-docs --family-key nc-progress-leaf-602
python -m duke_rates workflow run-nc-missing-doc --family-key nc-progress-leaf-602
python -m duke_rates workflow show-nc-missing-doc-status --family-key nc-progress-leaf-602
python -m duke_rates workflow report-nc-missing-doc-deferred
python -m duke_rates workflow plan-nc-missing-doc-remediation
python -m duke_rates workflow remediate-and-promote-nc-missing-docs
```

### Export DEP residential history

```powershell
python scripts/exports/export_dep_res_history.py
```

Writes, among other files:

- [dep_res_base_history.csv](c:/Python/Duke/Standalone/data/processed/dep_res_history/dep_res_base_history.csv)
- [dep_res_rider_totals.csv](c:/Python/Duke/Standalone/data/processed/dep_res_history/dep_res_rider_totals.csv)
- [dep_res_all_in_history.csv](c:/Python/Duke/Standalone/data/processed/dep_res_history/dep_res_all_in_history.csv)
- [dep_res_validation_summary.json](c:/Python/Duke/Standalone/data/processed/dep_res_history/dep_res_validation_summary.json)

### Export DEC residential history

```powershell
python scripts/exports/export_dec_rs_history.py
```

Writes, among other files:

- [dec_rs_base_history.csv](c:/Python/Duke/Standalone/data/processed/dec_rs_history/dec_rs_base_history.csv)
- [dec_rs_rider_totals.csv](c:/Python/Duke/Standalone/data/processed/dec_rs_history/dec_rs_rider_totals.csv)
- [dec_rs_all_in_history.csv](c:/Python/Duke/Standalone/data/processed/dec_rs_history/dec_rs_all_in_history.csv)
- [dec_rs_validation_summary.json](c:/Python/Duke/Standalone/data/processed/dec_rs_history/dec_rs_validation_summary.json)

### Export the canonical DEP vs DEC timeline

```powershell
python scripts/exports/export_canonical_residential_timeline.py
```

Output:

- [canonical_residential_timeline.csv](c:/Python/Duke/Standalone/data/processed/canonical_residential/canonical_residential_timeline.csv)

### Run the Streamlit comparison app

```powershell
python -m streamlit run streamlit_res_comparison_app.py
```

The app now loads from the cached canonical CSV by default and exposes:

- utility filters
- date filters
- metric selection
- confidence / provenance filters
- CSV export buttons
- combined DEP vs DEC timeline charts

### Parse actual bills and validate billing accuracy

```powershell
duke-rates parse-bills "..\version_3\Actual Duke Bills"
duke-rates derive-bill-observations
python scripts/exports/export_actual_bill_accuracy.py
```

Outputs:

- [progress_nc_actual_bill_accuracy.csv](c:/Python/Duke/Standalone/data/processed/bill_accuracy/progress_nc_actual_bill_accuracy.csv)
- [progress_nc_actual_bill_accuracy_summary.json](c:/Python/Duke/Standalone/data/processed/bill_accuracy/progress_nc_actual_bill_accuracy_summary.json)

Current saved-bill validation summary:

- `12` bills checked
- `12` `good_match`
- max absolute delta `$0.45`

### Rider date audit

```powershell
python scripts/exports/export_dep_rider_date_audit.py
```

Outputs:

- [dep_res_rider_date_audit_summary.json](c:/Python/Duke/Standalone/data/processed/dep_res_history/dep_res_rider_date_audit_summary.json)
- [dep_res_rider_component_date_completeness.csv](c:/Python/Duke/Standalone/data/processed/dep_res_history/dep_res_rider_component_date_completeness.csv)
- [dep_res_rider_component_date_matrix.csv](c:/Python/Duke/Standalone/data/processed/dep_res_history/dep_res_rider_component_date_matrix.csv)

## NCUC / Historical Recovery

Authenticated NCUC access is still important for source acquisition, but it is no longer the only meaningful source for NC:

- NCUC portal / docket PDFs
- local Duke-hosted current docs in `data/raw/nc/...`
- local historical/manual imports in `data/historical/raw/nc/...`
- bill-backed validation

Useful commands:

```powershell
duke-rates ncuc-login-test
duke-rates ncuc-resolve-docket-ids --all-seeded
duke-rates ncuc-docket-fetch <GUID> --download
duke-rates search run --utility progress --schedules 602 --portal
duke-rates audit-local-raw-nc --company progress
duke-rates audit-local-raw-nc --company carolinas
duke-rates load-local-rider-summaries-nc --company progress
duke-rates load-local-rider-summaries-nc --company carolinas
duke-rates load-local-rates-nc --company progress
duke-rates load-local-rates-nc --company carolinas
```

## Current NC Billing / Analytics Status

### DEP `RES`

- Current all-in timeline is usable across the `2016–2026` working range
- Pre-`2023-10-01` rider history remains partially provisional, but it is persisted and surfaced explicitly
- Clean post-`2023-10-01` rider-summary history is in the main summary tables
- Component-level rider dates are mostly tracked and auditable

### DEC `RS`

- Residential timeline is usable for comparison
- Earlier periods rely more on carry-forward coverage than DEP
- Current comparison exports and app surface that provenance explicitly

### Bills / reconciliation

- `RES` bills reconcile cleanly
- `R-TOU-CPP` parsing and validation are now usable
- Storm rider handling includes:
  - `Leaf 607`
  - `Leaf 613`
  - stacked behavior
  - mid-bill proration in reconciliation

## Recommended Handoff Starting Points

If another model is taking over work, it should usually start from:

1. [README.md](c:/Python/Duke/Standalone/README.md)
2. [docs/architecture.md](c:/Python/Duke/Standalone/docs/architecture.md)
3. [docs/known_issues.md](c:/Python/Duke/Standalone/docs/known_issues.md)
4. [docs/roadmap.md](c:/Python/Duke/Standalone/docs/roadmap.md)

Then, depending on task:

- data/history task:
  - [export_dep_res_history.py](c:/Python/Duke/Standalone/scripts/exports/export_dep_res_history.py)
  - [export_dec_rs_history.py](c:/Python/Duke/Standalone/scripts/exports/export_dec_rs_history.py)
  - [canonical_residential_timeline.csv](c:/Python/Duke/Standalone/data/processed/canonical_residential/canonical_residential_timeline.csv)
- billing task:
  - [reconciliation.py](c:/Python/Duke/Standalone/src/duke_rates/billing/reconciliation.py)
  - [riders.py](c:/Python/Duke/Standalone/src/duke_rates/billing/riders.py)
  - [tou.py](c:/Python/Duke/Standalone/src/duke_rates/billing/tou.py)
  - [progress_nc_actual_bill_accuracy_summary.json](c:/Python/Duke/Standalone/data/processed/bill_accuracy/progress_nc_actual_bill_accuracy_summary.json)
- app / analytics task:
  - [streamlit_res_comparison_app.py](c:/Python/Duke/Standalone/streamlit_res_comparison_app.py)
  - [plotly.py](c:/Python/Duke/Standalone/src/duke_rates/charts/plotly.py)
  - [canonical_residential.py](c:/Python/Duke/Standalone/src/duke_rates/analytics/canonical_residential.py)

## Development

```powershell
pytest
ruff check .
```

## Important Caveats

- The NC residential path is the most mature path in the repository.
- The broader all-states vision still exists, but handoff decisions should not assume equal maturity across all jurisdictions.
- The canonical residential timeline and bill-validation exports are the fastest way to understand the current practical state of the project.
