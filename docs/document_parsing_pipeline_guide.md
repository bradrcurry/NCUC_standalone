# Document Parsing Pipeline Guide

This guide is the operational entry point for the historical document parsing
pipeline.

If you are a human operator or another AI agent, start here before scanning the
codebase or digging through historical session reports.

For the repo-level onboarding path, also read
[AGENT_ONBOARDING.md](/c:/Python/Duke/Standalone/AGENT_ONBOARDING.md).

## Purpose

Use this guide to understand:

- what the historical parsing pipeline is responsible for
- which CLI commands map to each stage
- which workflow to use for a given task
- how to review weak parses, OCR work, and targeted reparsing
- which docs to read next only if you need more depth

## What The Pipeline Does

The current historical pipeline is centered on North Carolina NCUC documents.
Its main job is to move from discovered/downloaded filings to structured,
reviewable historical tariff data.

At a high level it does this:

1. discover candidate NCUC records
2. download PDFs
3. triage and mine page/span evidence
4. match bounded spans to tariff families
5. create `historical_documents` / `tariff_versions`
6. extract charges with parser profiles
7. queue weak, stale, OCR-heavy, or profile-impacted documents for targeted rework

This is not the same as the older `ingest-ncuc` JSON workflow. That legacy path
still exists, but the recommended path for historical NCUC work is the page-aware
pipeline described here.

## Read This First

For most work, an agent should read only these docs first:

1. [AGENT_ONBOARDING.md](/c:/Python/Duke/Standalone/AGENT_ONBOARDING.md)
2. [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md)
3. this guide
4. [agent_tool_use_policy.md](/c:/Python/Duke/Standalone/docs/agent_tool_use_policy.md)
5. [agent_task_routing.md](/c:/Python/Duke/Standalone/docs/agent_task_routing.md)
6. [source_of_truth_and_legacy_paths.md](/c:/Python/Duke/Standalone/docs/source_of_truth_and_legacy_paths.md)

Read these only when needed:

- [ncuc_pipeline_overview.md](/c:/Python/Duke/Standalone/docs/ncuc_pipeline_overview.md)
  Use when you need the fuller historical pipeline architecture and current
  implementation direction.
- [historical_parser_architecture.md](/c:/Python/Duke/Standalone/docs/historical_parser_architecture.md)
  Use when touching parser-profile selection, fallback behavior, or new profile design.
- [OCR_IMPLEMENTATION_PLAN.md](/c:/Python/Duke/Standalone/docs/OCR_IMPLEMENTATION_PLAN.md)
  Use when working on OCR routing, OCR queueing, or GPU escalation ideas.
- [docling_integration_plan.md](/c:/Python/Duke/Standalone/docs/docling_integration_plan.md)
  Use when working on planned Docling-based OCR/layout/table analysis and
  CPU-vs-CUDA pilot evaluation.
- [document_intelligence_architecture.md](/c:/Python/Duke/Standalone/docs/document_intelligence_architecture.md)
  Use when extending the new normalized document-representation, fingerprinting,
  validation, OCR/backend routing, and ML-dataset capture layer that now wraps
  the historical bulk extractor.
- [architecture.md](/c:/Python/Duke/Standalone/docs/architecture.md)
  Use when you need broader repo/database context.
- [roadmap.md](/c:/Python/Duke/Standalone/docs/roadmap.md)
  Use when deciding what to implement next.
- [agent_change_checklist.md](/c:/Python/Duke/Standalone/docs/agent_change_checklist.md)
  Use before closing work so handoff quality stays consistent across agents.
- [knowledge_capture_workflow.md](/c:/Python/Duke/Standalone/docs/knowledge_capture_workflow.md)
  Use when deciding what must be promoted into canonical docs versus left in dated reports.
- [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md)
  Use when you need the sanctioned default path for operating or improving the current workflow surface.
- [agent_tool_use_policy.md](/c:/Python/Duke/Standalone/docs/agent_tool_use_policy.md)
  Use when choosing between existing local tools, helper promotion, and one-off workarounds.

Avoid treating `docs/reports/` as the primary source of truth for operation.
Those reports are useful context, but they are not the shortest path for using
the current pipeline correctly.

## Command Map

The primary operator interface should be the CLI commands below, not ad hoc
one-off scripts.

There are some helper scripts under `scripts/debug/` and `scripts/maintenance/`,
but several of them are still hard-coded to specific record IDs, paths, or local
inspection tasks. Treat those as developer utilities unless they are later
promoted to documented CLI commands.

### Discovery and download

Use these to populate `ncuc_discovery_records` and fetch PDFs:

```powershell
python -m duke_rates ncuc-seed-discover
python -m duke_rates ncuc-search "Duke Energy Progress rate schedule 605"
python -m duke_rates ncuc-fetch --pending
python -m duke_rates ncuc-fetch-portal --limit 50
python -m duke_rates ncuc-list --status success --limit 20
python -m duke_rates ncuc-show 1234
```

### Import and page-aware mining

Use these to push downloaded records into the historical pipeline:

```powershell
python -m duke_rates ncuc-import-pipeline --all-downloaded
python -m duke_rates ncuc-import-pipeline --record-id 1234
```

Default to `ncuc-import-pipeline`.
`mine-ncuc-pipeline` is retained as a compatibility alias for the same
page-aware intake path and is mainly useful when another workflow still refers
to the older name.

### Missing clean-document recovery

Use these when the family is known but the clean historical tariff/rider PDF is
still missing or weakly represented:

```powershell
python -m duke_rates search-nc-missing-clean-docs --family-key nc-progress-leaf-602
python -m duke_rates run-nc-missing-doc-workflow --family-key nc-progress-leaf-602
python -m duke_rates report-nc-missing-doc-triage --family-key nc-progress-leaf-602 --actionable-only --top 10
python -m duke_rates execute-top-nc-missing-doc-triage --family-key nc-progress-leaf-602
python -m duke_rates show-nc-missing-doc-status --family-key nc-progress-leaf-602
python -m duke_rates report-nc-missing-doc-deferred
python -m duke_rates plan-nc-missing-doc-remediation
```

The workflow command is resumable. It is intended to close the loop from:

1. search candidate NCUC rows
2. fetch/download eligible records
3. import the historical document
4. bootstrap any missing `tariff_version`
5. queue the imported slice for targeted reprocessing
6. persist triage guidance with ranked `next_action` recommendations

Search escalation for difficult missing documents:

1. exact docket structured search
2. nearby docket expansion when the exact label is weak or absent
3. docketless broad structured search
4. richer keyword fan-out using schedule code, title, leaf, redline, and
   multiple docket hints
5. tolerant docket-id resolution that accepts normalized or near matches instead
   of only exact visible-text labels

For weaker agents, the preferred recovery loop is:

1. run `run-nc-missing-doc-workflow`
2. read `report-nc-missing-doc-triage --actionable-only --top 10`
3. either execute one step with `execute-top-nc-missing-doc-triage`
4. or execute a bounded batch with `execute-batch-nc-missing-doc-triage --max-actions N`

When a target stalls for a supported reason, use:

```powershell
python -m duke_rates remediate-nc-missing-doc-no-download-url
python -m duke_rates remediate-nc-missing-doc-effective-start
python -m duke_rates remediate-nc-missing-doc-confidence
python -m duke_rates remediate-and-promote-nc-missing-docs
python -m duke_rates promote-nc-missing-doc-targets
```

Use `show-nc-missing-doc-status` before manual intervention so the family-level
state, provenance, and deferred reason are all visible in one place.
Use `report-nc-missing-doc-triage` when you need the persisted queue of what to
do next, and prefer its ranked actionable view over reconstructing triage from
raw status output.

Compact `next_action` guide:

| `next_action` | Use this |
|---|---|
| `fetch_document` | `run-nc-missing-doc-workflow --from-stage fetch --to-stage fetch ...` |
| `retry_fetch_or_manual_portal_review` | `run-nc-missing-doc-workflow --from-stage fetch --to-stage fetch --retry-failed-fetch ...` |
| `import_and_mine_document` | `run-nc-missing-doc-workflow --from-stage import --to-stage import ...` |
| `bootstrap_tariff_version` | `run-nc-missing-doc-workflow --from-stage bootstrap_versions --to-stage bootstrap_versions ...` |
| `process_document` / `retry_with_better_parser_context` | `run-nc-missing-doc-workflow --from-stage queue_reprocess --to-stage process_reprocess ...` |
| `review_*`, `ready_for_acceptance`, `wait_for_reprocess_completion`, `monitor_linked_document` | `show-nc-missing-doc-status ...` |

The ranked triage report and both triage executor commands already follow this
mapping. Use them instead of re-deriving it in ad hoc session logic.

### Bulk charge extraction and validation

Use these after historical documents and tariff versions exist:

```powershell
python -m duke_rates extract-rates-nc
python -m duke_rates validate-extraction-nc
python -m duke_rates test-bill-reconstruction-nc
```

`extract-rates-nc` now targets only historical documents that already have a
linked `tariff_version`. The command also reports how many historical documents
were skipped before extraction because that linkage is still missing, so a run
cannot look successful while doing only no-op warnings.

Parser-selection note from the April 21, 2026 tuning pass:

- unsupported families now land on `unknown` instead of defaulting to
  `generic_residential`
- known formula-driven program leaves such as `701`, `702`, `708`, `719`, and
  `720` can now skip as formula-only from title/family signals even when
  OCR/plaintext is missing
- extractable incentive leaves like `715` and `725` still need usable
  OCR/plaintext; title-only hints are not enough to safely fabricate charges

### Review and diagnostics

Use these to inspect weak parses and attach review outcomes:

```powershell
python -m duke_rates parse-review-queue
python -m duke_rates parse-review-summary
python -m duke_rates reconcile-skipped-parse-reviews
python -m duke_rates record-parse-review 123 corrected --notes "Adjusted TOU period mapping"
```

Current behavior:

- `parse-review-queue` and `parse-review-summary` now count only the latest
  parse attempt per `source_pdf + page range + parser_stage`, which makes the
  backlog operational instead of cumulative over every historical rerun.
- `enqueue-reprocess-nc --family-key ...` and other targeted requeue filters
  now apply the filter before truncating to the queue limit. Use a high limit
  for family-specific cleanup passes, but the command no longer silently misses
  a family just because unrelated weak rows ranked ahead of it.
- `reconcile-skipped-parse-reviews` is a repair command for older rows that
  were still marked `needs_review` even though the latest parse attempt is now
  a legitimate `skipped_*` outcome.

### Targeted reprocessing

Use these instead of rerunning everything:

```powershell
python -m duke_rates enqueue-reprocess-nc
python -m duke_rates show-reprocess-queue-nc
python -m duke_rates process-reprocess-queue-nc
```

Current behavior:

- if a queued historical document is missing its `tariff_version`, the queue
  processor now bootstraps the minimal historical version row instead of
  failing immediately
- if the queued reason is a stale/missing page or span stage, the queue
  processor now refreshes those artifacts before rerunning extraction
- recent targeted backlog reduction work has also cleared:
  - `nc-progress-leaf-672` via broader formula-only `Rider CEI` detection
  - `nc-carolinas-doc-SCHEDULEPLSTREETANDPUBLICLIGHTINGSERVICE` and
    `nc-carolinas-doc-SCHEDULEFLFLOODLIGHTINGSERVICE` via the
    `carolinas_lighting_schedule` profile
  - `nc-carolinas-doc-SCHEDULEOPTE`, `nc-carolinas-schedule-TS`, and
    `nc-carolinas-doc-SCHEDULEWC` via the `carolinas_schedule_bridge`
    profile plus shared Carolinas `Basic Facilities Charge` parsing support
  - `nc-carolinas-rider-SCG` via the
    `carolinas_small_customer_generator` profile plus narrow
    terms-only/reference skips for non-rate continuation pages

### Stale-stage and profile-impact reparsing

Use these when the pipeline changed and you want selective invalidation:

```powershell
python -m duke_rates show-stale-historical-nc
python -m duke_rates enqueue-stale-reprocess-nc
python -m duke_rates show-profile-impact-nc --parser-profile progress_residential_tou
python -m duke_rates enqueue-profile-impact-nc --parser-profile progress_residential_tou
python -m duke_rates show-profile-impact-nc --parser-profile progress_billing_adjustments
python -m duke_rates enqueue-profile-impact-nc --parser-profile progress_billing_adjustments
```

Practical note:

- `show-stale-historical-nc` is now expected to fall to `0` after a successful
  stale reprocess cycle under the current stage versions
- if it does not, treat that as a real bug or unsupported stage dependency, not
  as expected noise

### OCR queue

Use these for scanned documents and OCR-required work:

```powershell
python -m duke_rates enqueue-ocr-nc
python -m duke_rates show-ocr-queue-nc
python -m duke_rates show-ocr-remediation-candidates-nc
python -m duke_rates enqueue-ocr-remediation-nc --limit 10
python -m duke_rates process-ocr-queue-nc
python -m duke_rates process-ocr-queue-nc --workers 2
```

Prerequisites:

- install the Python OCR extras for this repo
- install the system Tesseract binary and ensure it is on `PATH`
- treat this queue as the CPU OCR baseline, not the only OCR path

Recommended routing:

1. use the normal import/extract path first
2. if the document is clearly scanned or native text is sparse, use the OCR queue
3. if OCR text exists but layout/table quality is still weak, escalate to the
   document-intelligence normalizer or Docling
4. if the issue is only a few suspicious pages, prefer page-level escalation
   over whole-document heavy OCR

Triage surfaces:

- `show-ocr-remediation-candidates-nc` is the first audit for `unknown` /
  no-text / weak-without-OCR cohorts
- `enqueue-ocr-remediation-nc` is the direct bridge from that audit into the
  OCR queue for the `queue_ocr_or_paddle` subset
- `report-ocr-benchmark-nc` is useful only after OCR artifacts exist and you
  want backend/outcome cohort reporting
- `process-ocr-queue-nc --workers N` is safe for bounded parallel local OCR;
  do not generalize that concurrency to authenticated portal/search work
- `process-reprocess-queue-nc --workers N` is also safe for bounded parallel
  local reparsing; keep remote portal/search steps sequential

### Document-intelligence normalization path

The bulk extractor now also runs an additive document-intelligence
normalization step before fingerprinting/schema validation/training capture.

That normalization layer currently supports:

- native/page-artifact text reuse
- optional Paddle `PP-Structure` normalization
- optional Ollama `glm-ocr` page fallback

Operational guidance:

- do not replace the existing pipeline with ad hoc OCR scripts
- prefer improving the normalization router/backends when OCR/layout quality is weak
- keep native extraction as the cheapest viable path
- use Paddle as the primary OCR/layout backend
- use GLM-OCR selectively for difficult low-text pages and for native-text pages with suspicious symbol noise like `cVkWh`, `S/kWh`, or merged numeric strings
- treat redline/blackline filings as special analysis candidates rather than ordinary tariff leaves; the fingerprinter now flags them explicitly for higher-touch review

Recommended layered strategy:

1. `NativePdfNormalizer` / page artifacts
   Use for clean PDFs or any document that already has a usable text layer.
2. `PaddleStructureNormalizer`
   Use when the document is scanned or layout-heavy and table/block structure
   matters.
3. page-level GLM-OCR escalation
   Use when only a subset of pages are low-text or corrupted by symbol noise.
4. Docling
   Use for the hardest cases: compliance books, mixed-layout spans, repeated
   weak/empty parses, or relationship/explanation work.

Selection rule:

- prefer the cheapest backend that yields usable normalized evidence
- do not send clean native-text PDFs into OCR/Docling by default
- escalate when the failure mode is text quality or layout quality, not when
  the real issue is family linkage or missing source documents

Current practical conclusion from local benchmarks:

- native text first is still the right default
- GLM is a selective fallback, not a default pipeline
- Paddle is the intended structured OCR/layout backend, but its translation
  layer still needs improvement before it becomes the universal escalation path

### Docling bridge path (operational)

Docling is an optional heavy-analysis backend for:
- OCR-heavy scans
- table-heavy rider summaries and compliance books
- weak or empty historical parses
- future LLM-assisted explanation / relationship work

Docling artifacts are stored in the database with deterministic JSON and plain-text content.

Bridge workflow:

```powershell
python -m duke_rates mine-docling-nc --limit 50 --accelerator cuda
python -m duke_rates mine-docling-nc --dry-run --limit 10     # Preview first
```

The bridge:
- reconstructs `PageEvidence` from stored Docling JSON
- reuses existing text-based feature extraction
- segments pages into `TariffSpan`s using existing logic
- matches spans to families using existing family matcher
- creates historical documents using existing extraction pipeline
- keeps native-text and OCR paths as the fast default
- caches Docling artifacts by file hash and backend version
- preserves deterministic tariff extraction as the source of truth

This is operational work done on-demand, not injected into the default pipeline.

### Provisional family review

Use these when the importer preserved a strong unmatched tariff span as a
provisional historical family:

```powershell
python -m duke_rates list-provisional-families
python -m duke_rates promote-provisional-family nc-carolinas-program-SMARTENERGYNOWPROGRAM --alias "SMART ENERGY NOW PROGRAM"
python -m duke_rates list-historical-only-families --state NC --company carolinas
python -m duke_rates list-historical-only-families --state NC --company carolinas --only-unresolved
python -m duke_rates attach-current-document-to-family nc-carolinas-program-SMARTENERGYNOWPROGRAM 96
```

The historical-only command now includes candidate current-document suggestions
when available. Suggestions now incorporate first-page mined evidence when
metadata alone is weak, including recovered headings and leaf numbers from the
candidate current PDF, and they now prefer continuity with known historical
leaf numbers when that evidence exists. Treat those as review hints, not
automatic matches.
Use `--only-unresolved` when you want the shorter queue of historical families
that still have no plausible current anchor at all.
Use `attach-current-document-to-family` only after a reviewer decides the
candidate is actually the right anchor.

### Current anchor mismatch audit

Use this when a tariff family has a current `documents.id` anchor, but the
family metadata may contradict the anchored current PDF:

```powershell
python -m duke_rates list-current-anchor-mismatches --state NC --company progress
python -m duke_rates sync-family-metadata-from-current-anchor nc-progress-leaf-501
```

This compares family metadata against the anchored current document's
`schedule_code`, `tariff_identifier`, and first-page mined headings/leaf
numbers. It is useful for surfacing catalog contradictions such as an older
family model still labeling a leaf as `FUEL` while the anchored current PDF is
clearly `R-TOUD`. Use `sync-family-metadata-from-current-anchor` only when the
current document anchor itself is already trusted and the problem is stale
family metadata rather than a bad current-document linkage.
That low-risk sync path has already been used successfully on:
- `nc-progress-leaf-609`
- `nc-progress-leaf-662`
- `nc-progress-leaf-670`
The remaining DEP migration-review cases have now been split into separate
current vs historical families:
- `nc-progress-leaf-501` + `nc-progress-doc-FUELCHARGEADJUSTMENT`
- `nc-progress-leaf-504` + `nc-progress-doc-RESIDENTIALTIMEOFUSEENERGY`
- `nc-progress-leaf-607` + `nc-progress-doc-STORMRECOVERYRIDER`

That leaves the live DEP current-anchor mismatch queue at `0`.

### Historical family mismatch audit

Use this when historical documents appear to be attached to the wrong family
even though the parser itself is behaving correctly:

```powershell
python -m duke_rates show-lineage-gaps-nc
python -m duke_rates validate-lineage-nc
python -m duke_rates show-provenance-gaps-nc
python -m duke_rates suggest-family-links-nc --limit 25
python scripts/maintenance/audit_historical_family_mismatches.py --family-key nc-progress-leaf-500
python scripts/maintenance/audit_historical_family_mismatches.py --family-key nc-progress-leaf-500 --apply-purge
```

This audit compares bounded historical PDF text against the assigned family's
expected company and schedule code. It is useful for identifying legacy
contamination where a document was matched to the wrong family years ago and is
now showing up as a weak parser case.
It now also catches summary-sheet lineage mistakes, such as a DEP
`SUMMARY OF RIDER ADJUSTMENTS` document being attached to
`nc-progress-leaf-601` instead of `nc-progress-leaf-600`.

Preferred repair sequence:

```powershell
python scripts/maintenance/audit_historical_family_mismatches.py --family-key nc-progress-leaf-601
python scripts/maintenance/audit_historical_family_mismatches.py --family-key nc-progress-leaf-601 --apply-purge
python -m duke_rates mine-ncuc-pipeline --record-id 1245
python -m duke_rates enqueue-profile-impact-nc --parser-profile progress_rider_adjustment_matrix --family-key nc-progress-leaf-600
python -m duke_rates process-reprocess-queue-nc --limit 5
```

### Reference-only historical documents

Some historical files are tariff-adjacent but are not bill-rate extraction
targets, for example:

- program terms/pilot participation sheets
- line extension plans
- service-regulation documents that are mostly contractual language

When those documents are clearly non-billable in structure, the historical
extractor can now exit them as `skipped_reference`. Those skips are treated as
accepted rule outcomes, not `needs_review`. Do not try to force every such
document into `generic_residential` just because it belongs to a tariff family.

### Rider BA historical parsing

`nc-progress-leaf-601` now has a dedicated historical parser profile:
`progress_billing_adjustments`.

Use profile-impact reprocessing if you change that parser or its selection
rules:

```powershell
python -m duke_rates show-profile-impact-nc --parser-profile progress_billing_adjustments
python -m duke_rates enqueue-profile-impact-nc --parser-profile progress_billing_adjustments
python -m duke_rates process-reprocess-queue-nc
```

Current practical note:

- true Rider BA sheets are now expected to parse into adjustment charges
- if a `leaf-601` historical document still lands as empty/generic, inspect
  whether it is actually a `leaf-600` summary span or another mismatched
  historical attachment before writing more BA parsing code

## Operator Tools That Matter

If you are operating the pipeline, these are the current tools that are actually
worth relying on:

### 1. The CLI command surface

This is the main operator interface and is the most important existing tool.

Use it for:

- discovery and download
- import and mining
- extraction and validation
- review queue inspection
- OCR queue processing
- stale-stage and profile-impact reparsing
- provisional family review/promotion
- historical-only family review
- current-anchor mismatch review

### 2. Review and reprocess queues

These are the key operator helpers already built into the system.

Use them to answer:

- what is weak right now?
- what needs review?
- what changed because of a parser/OCR/stage update?
- what should be rerun without sweeping the archive?

The most useful commands are:

```powershell
python -m duke_rates parse-review-summary
python -m duke_rates parse-review-queue
python -m duke_rates show-reprocess-queue-nc
python -m duke_rates show-reprocess-priority-nc
python -m duke_rates show-stale-historical-nc
python -m duke_rates show-profile-impact-nc --parser-profile progress_residential_tou
python -m duke_rates show-ocr-queue-nc
python -m duke_rates list-provisional-families
python -m duke_rates list-historical-only-families
python -m duke_rates list-current-anchor-mismatches
```

### 3. OCR/cache/reprocess state in SQLite

For deeper inspection, the DB-backed state is already a useful operator tool.
Important tables include:

- `parse_attempt_logs`
- `parse_review_outcomes`
- `historical_processing_runs`
- `historical_reprocess_queue`
- `ocr_processing_queue`
- `ocr_artifacts`
- `document_fingerprints`
- `ncuc_page_artifacts`
- `ncuc_span_artifacts`

Useful summary commands:

- `python -m duke_rates show-provenance-gaps-nc`
- `python -m duke_rates show-fingerprint-coverage-nc`
- `python -m duke_rates show-document-classification-audit-nc`

These are better operator anchors than broad rescans, because they tell you what
the pipeline already knows and what it thinks is weak or stale.

Use `show-document-classification-audit-nc` when the bigger question is not
"which parser profile fired?" but "what kind of document is this really?".
That audit is the current best surface for splitting the corpus into:

- extractable tariff-charge documents
- formula-only documents
- reference/procedural documents
- redline candidates
- unrelated-but-keep documents that may matter later
- true unknowns that still need better routing

Use `show-unknown-routing-audit-nc` after that when the problem is specifically
which unsupported families deserve:

- a new parser profile
- a formula/program lane instead of charge extraction
- reclassification into reference/procedural content

If another AI model needs one compact parser-improvement surface instead of
three separate audits, start with:

- `python -m duke_rates show-parser-improvement-candidates-nc --limit 25`

That command is the current best queue for:

- which family to work next
- whether the likely fix is profile/routing/formula/reference
- which sanctioned follow-up command to run

## Existing Scripts: Adequate vs Ad Hoc

### Good enough as maintenance helpers

- `scripts/maintenance/repair_ncuc_company_mismatches.py`
- `scripts/maintenance/verify_pdfs.py`
- `scripts/maintenance/check_docs.py`

These are still secondary to the CLI, but they are legitimate maintenance tools.

### Ad hoc and not ideal as operator tools

- `scripts/debug/debug_triage.py`
- `scripts/debug/inspect_db.py`
- `scripts/debug/inspect_db_comparison.py`
- `scripts/debug/analyze_dates.py`
- `scripts/debug/run_queries.py`

These are useful for local investigation, but they are not yet robust operator
tools because some are hard-coded to:

- specific record IDs
- local DB paths
- narrow one-off inspection tasks

If another AI agent needs a stable operator path, it should not start with those.

## If An Agent Creates A New Helper

If an AI agent has to create a bespoke helper to operate this pipeline, assume
that one of two things is true:

- the current operator surface is missing a needed capability
- the helper may be useful to future operators and agents

Do not leave that helper as unnamed residue in the repo root or buried in a
session note.

Also update [agent_tool_use_policy.md](/c:/Python/Duke/Standalone/docs/agent_tool_use_policy.md)
if the change affects tool-choice rules or promotion guidance.

### Where the helper should go

- repeated operator workflow -> promote to a documented CLI command
- reusable maintenance/backfill/repair task -> `scripts/maintenance/`
- reusable export/report task -> `scripts/exports/`
- narrow investigation aid that is not yet stable enough for the CLI ->
  `scripts/debug/`
- ingest/backfill runner -> `scripts/ingestion/`

### Minimum documentation standard

Any kept helper should make these things clear:

- purpose
- expected inputs
- expected outputs or side effects
- safe usage pattern
- whether it is a temporary investigation aid or a stable operator tool

At minimum, add or update documentation in this guide when a helper becomes part
of the recommended workflow.

### When to promote a helper into the CLI

Promote a helper out of `scripts/debug/` and into the CLI when one or more of
these become true:

- multiple agents are likely to need it
- it is parameterized enough to be reused safely
- it answers a recurring operator question
- it is part of routine review, OCR, reprocess, or validation work
- leaving it as a script would force future agents to rediscover the workflow

### What not to do

- do not leave one-off helpers in the repo root
- do not create hard-coded scripts without documenting their scope
- do not assume a helper is disposable just because it started as ad hoc code
- do not let important operational knowledge live only in session transcripts

## Recommended Tooling Improvements

The following helpers would materially improve pipeline operation:

### 1. Pipeline doctor / health summary

A single command that summarizes:

- pending downloads
- pending OCR queue items
- pending reprocess queue items
- stale-stage counts
- weak parse counts
- top corrected parser profiles

This would likely be the best next operator helper.

### 2. Document lineage / relationship inspector

A command that starts from one item and shows related artifacts:

- source PDF
- discovery record
- historical document
- tariff version
- parse attempts
- review outcomes
- queue entries
- related family/rider/docket links

This would be especially useful once the relationship-map layer is implemented.

### 3. Single-document explain command

A command that explains, for one record or source PDF:

- triage result
- parser profile chosen
- fallback recommendations
- review flags
- why it was or was not reparsed
- whether OCR exists

That would make the system much easier for another agent to operate.

### 4. Promote ad hoc debug scripts into parameterized CLI commands

The existing debug scripts show that these needs are real. The ideal next step is
not more hard-coded scripts. It is promoting the useful ones into stable,
parameterized commands.

## Recommended Agent Practice

If you are another AI agent:

- prefer the CLI and DB-backed queues over direct script execution
- use `scripts/debug/*` only when the CLI truly lacks the needed inspection view
- if you find yourself repeating a debug script workflow, convert it into a CLI
  command or document it here
- if you create a helper that may be reused, move it into the correct `scripts/`
  subfolder and document it here
- do not assume a script under `scripts/debug/` is production-safe or general-purpose

## Recommended Workflows

### Workflow A: Process newly downloaded NCUC documents

Use this when new discovery records or downloads have arrived.

```powershell
python -m duke_rates ncuc-fetch --pending
python -m duke_rates ncuc-import-pipeline --all-downloaded
python -m duke_rates extract-rates-nc
python -m duke_rates validate-extraction-nc
python -m duke_rates parse-review-summary
```

When to use:

- normal incremental ingest
- after a batch of portal downloads
- after bringing in a new docket search result set

### Workflow B: Reprocess only weak historical parses

Use this when the pipeline is already populated and you want to improve quality
without sweeping the full archive.

```powershell
python -m duke_rates parse-review-summary
python -m duke_rates enqueue-reprocess-nc
python -m duke_rates show-reprocess-queue-nc
python -m duke_rates process-reprocess-queue-nc
python -m duke_rates parse-review-summary
```

When to use:

- parser improvements landed
- weak parses are accumulating
- you want to improve extraction quality selectively

### Workflow C: Reprocess because the pipeline itself changed

Use this after changing OCR/page/span/parser stages or specific parser profiles.

For stage-version changes:

```powershell
python -m duke_rates show-stale-historical-nc
python -m duke_rates enqueue-stale-reprocess-nc
python -m duke_rates process-reprocess-queue-nc
```

For parser-profile-specific changes:

```powershell
python -m duke_rates show-profile-impact-nc --parser-profile carolinas_rider_adjustment_matrix
python -m duke_rates enqueue-profile-impact-nc --parser-profile carolinas_rider_adjustment_matrix
python -m duke_rates process-reprocess-queue-nc
```

When to use:

- a parser profile changed
- OCR backend version changed
- page/span artifact version changed
- you want selective reruns with the new logic

### Workflow D: Handle scanned PDFs

Use this when many records are `OCR_REQUIRED` or low-text.

```powershell
python -m duke_rates enqueue-ocr-nc
python -m duke_rates show-ocr-queue-nc
python -m duke_rates process-ocr-queue-nc
python -m duke_rates enqueue-stale-reprocess-nc
python -m duke_rates process-reprocess-queue-nc
```

Important:

- OCR is CPU-first by default
- OCR output is cached
- OCR should feed back into the same page/span/parser flow

### Workflow E: Investigate a specific family, rider, or parser problem

Use this when troubleshooting one family or one parser cluster.

Suggested sequence:

```powershell
python -m duke_rates parse-review-summary
python -m duke_rates show-profile-impact-nc --parser-profile progress_rider_adjustment_matrix
python -m duke_rates enqueue-profile-impact-nc --parser-profile progress_rider_adjustment_matrix --family-key nc-progress-leaf-600
python -m duke_rates process-reprocess-queue-nc
```

Then inspect:

- parse-review queue
- latest affected `historical_documents`
- associated discovery records and source PDFs

## Recommended Agent Behavior

If you are another LLM agent working in this repo, use this approach:

1. Read this guide first.
2. Read [ncuc_pipeline_overview.md](/c:/Python/Duke/Standalone/docs/ncuc_pipeline_overview.md) for architecture and current limits.
3. Read [historical_parser_architecture.md](/c:/Python/Duke/Standalone/docs/historical_parser_architecture.md) if touching parser profiles.
4. Prefer targeted queue workflows over broad reruns.
5. Use review and reprocess tables as the operational source of “what needs work.”
6. Only read deeper reports or broad code areas if the task truly requires it.

In particular:

- do not start with a full repo scan
- do not rerun the entire archive by default
- do not treat OCR as a separate independent pipeline
- do not assume the legacy JSON ingest path is the preferred historical workflow

## How To Choose The Right Tool

Use `parse-review-*` when:

- you want to understand weak parses
- you need human review and correction context
- you want to prioritize parser fixes

Use `enqueue-reprocess-*` when:

- the goal is to improve existing weak results
- a parser or rule change landed

Use `show-stale-historical-nc` / `enqueue-stale-reprocess-nc` when:

- a stage version changed
- OCR/page/span/parser artifacts may now be outdated

Use `show-profile-impact-nc` / `enqueue-profile-impact-nc` when:

- a specific parser profile changed
- you want the narrowest possible rerun scope

Use `show-parser-selection-audit-nc` when:

- too many latest runs are ending `weak` or `empty`
- you need to see whether documents are really using a specialized profile or just falling into `generic_residential`
- you want a faster latest-run audit than the heavier review summaries

Selection note:

- unsupported documents now land on parser profile `unknown` instead of being mislabeled as `generic_residential`
- treat large `generic_residential` cohorts as true residential-style fallback usage, not as the generic “no profile matched” bucket

Use `list-weak-unbounded-historical-nc` when:

- weak historical rows still point at whole PDFs instead of bounded spans
- you need to separate parser-profile work on current PDFs from legacy lineage cleanup
- you want a fast view of whether the next action is:
  - `add_profile_or_current_parser_bridge`
  - `remine_from_discovery_record`
  - `retire_legacy_raw_attachment`
  - `retire_bundle_reference_residue`
  - `manual_lineage_review`

Use `list-redundant-legacy-raw-historical-nc` when:

- a legacy raw whole-PDF row already has a bounded regulator-PDF replacement
- you want to purge obsolete residue instead of writing another parser for it
- you are trying to reduce the weak-unbounded queue without over-purging rows
  that still need a real remine

Use `list-placeholder-heading-historical-nc` when:

- bounded Carolinas book spans look like `TYPE OF SERVICE` or
  `Effective for service` instead of a real schedule/rider family
- you need to identify heading fragments that should be retired as residue
  rather than parsed further
- you want a reversible review surface before deleting those rows

Use `retire-historical-document` when:

- a historical row has been confirmed as residue or contamination
- you need to delete the row and its attached parse/review/version state
  without direct SQLite surgery

Use OCR queue commands when:

- the document is scanned or low-text
- OCR work should be prepared or processed independently from normal extraction

## Current Guardrails

- Deterministic extraction remains the source of truth for authoritative tariff facts.
- Review outcomes and parse diagnostics should be stored, not left implicit in ad hoc notes.
- Relationship mapping and future LLM-assisted analysis should enrich the dataset, not silently overwrite parsed facts.
- Targeted reprocessing is preferred over broad rescans.

## Known Boundaries

- The historical page-aware path is strongest for NCUC work, not yet for all states.
- OCR support is real but still evolving.
- Relationship mapping and LLM-assisted document analysis are planned, not yet the operational default.
- The legacy `ingest-ncuc` JSON handoff still exists for compatibility, but it is not the preferred path for new historical pipeline work.
- Some historical rider leaves are intentionally single-value outputs. For
  example, Progress `leaf-608/609/610` rider sheets are expected to parse as a
  single adjustment charge under `progress_single_value_rider`; do not treat
  those as weak merely because the charge count is one.
- Do not trust long report-sized spans that have no leaf numbers and no real
  schedule/rider markers. The importer now skips those before family creation,
  because live testing showed they can otherwise reappear as false tariff docs
  from generic topic overlap alone.
- Use the historical family mismatch audit before assuming a weak family needs
  a new parser. Recent live cleanup of `leaf-535`, `leaf-649`, and `leaf-674`
  showed that several apparent parse failures were actually cross-company
  lineage mistakes.
- The remaining weak backlog now includes a distinct legacy-unbounded cohort.
  Many of those rows come from `data\\historical\\raw\\...` attachments that
  predate the bounded-span pipeline. Work them through
  `list-weak-unbounded-historical-nc` before adding new parser profiles.
- Weak legacy raw rows now infer `discovery_record_id` from their stored
  `local_file` metadata when possible, so many rows that used to look like
  `manual_lineage_review` now surface correctly as
  `remine_from_discovery_record`.
- Legacy raw attachments are now also used as conservative remine hints during
  importer family matching. The importer first tries per-span hint selection,
  so multi-family regulator PDFs like tariff bundles can still reuse legacy
  evidence without leaking one raw family across the whole document.
- Legacy raw rows can now also surface as `retire_legacy_raw_attachment` when
  the cached page evidence for the regulator PDF shows no real tariff
  structure. That is the expected path for old false-positive raw rows tied to
  procedural filings rather than actual tariff sheets.
- Generic provisional families such as `TYPEOFSERVICE` and
  `EFFECTIVEFORSERVICE` are no longer treated as normal supported matching
  targets during remine. That reduces false matches on book-style tariff PDFs
  that contain many valid schedule headings.
- Book-style regulator PDFs can now split on distinct schedule-title
  transitions even when they lack clean leaf headers. This was required to turn
  large DEP tariff books like `E-2, Sub 1142` from one giant span into bounded
  schedule sections.
- `list-weak-unbounded-historical-nc` is now bundle-aware:
  - rows like discovery `957` can surface as
    `retire_legacy_raw_attachment` when the regulator PDF has no real tariff
    structure and the old raw rows are just false-positive residue
  - rows like discovery `1124` can now surface as
    `retire_bundle_reference_residue` when cached span evidence shows the old
    raw rider rows appear only as rider-application references inside already
    bounded host spans
- Use `list-bundle-reference-legacy-raw-historical-nc` before purging those
  rows. It prints the host bounded documents and page ranges that justify the
  retirement decision.
- The remaining Progress legacy weak-unbounded backlog from `1124`
  (`602`, `605`, `610`, `718`) has now been retired from the live DB after the
  bundle-reference report proved they were not standalone rider sheets.
- The former `957` raw rows (`613`, `672`) have now been retired as
  procedural false positives after the queue learned to distinguish
  real tariff structure from rider mentions inside procedural filings.
- The current-PDF branch of that queue now has two dedicated DEP bridges:
  - `progress_current_leaf_bridge` for current-style leafs `501`, `520`, `535`, and `674`
  - `progress_specialty_rider` for specialty riders `654`, `655`, `668`, and `670`
- The current-PDF Carolinas branch now also has two dedicated profiles:
  - `carolinas_current_leaf_bridge` for `nc-carolinas-schedule-HLF`
  - `carolinas_solar_choice_rider` for `nc-carolinas-rider-NMB` and `nc-carolinas-rider-NSC`
- `repair-historical-current-snapshot` has now been used to fix the remaining
  Carolinas weak-unbounded current-PDF row (`nc-carolinas-schedule-PP`), so
  both the Progress and Carolinas weak-unbounded queues are currently `0`.
- `list-placeholder-heading-historical-nc` has now been used to retire `19`
  Carolinas heading-residue rows (`TYPE OF SERVICE` / `Effective for service`)
  from the live DB.
- After the latest live cleanup cycle:
  - `parse-review-summary --json` now reports `68` outstanding `needs_review`
  - review summary / queue output now resolves current historical lineage, so
    deleted historical docs and superseded reruns no longer inflate the active
    backlog
  - the remaining backlog is now dominated by real bounded
    `generic_residential` schedule spans rather than stale queue noise,
    placeholder residue, or duplicate reruns of the same historical document
- Recent Progress-specific cleanup also added dedicated historical profiles for:
  - `progress_sunsense_solar_rebate` on `nc-progress-leaf-716`
  - `progress_meter_related_optional_programs` on `nc-progress-leaf-661`
  - `progress_standby_service` on `nc-progress-leaf-653`
  - `progress_greenpower_program` on `nc-progress-leaf-642`
- Recent Carolinas-specific cleanup also added dedicated historical profiles for:
  - `carolinas_energy_efficiency_rider` on `nc-carolinas-rider-EE`
  - `carolinas_economic_development_rider` on `nc-carolinas-rider-EC`
  - `carolinas_interruptible_service_rider` on `nc-carolinas-rider-IS`
  - `green_source_advantage_rider` on `nc-carolinas-rider-GSA` and
    `nc-progress-leaf-665`
  - `carolinas_schedule_bridge` on `nc-carolinas-schedule-I`,
    `nc-carolinas-doc-SCHEDULEOPTE`, `nc-carolinas-schedule-TS`,
    and `nc-carolinas-doc-SCHEDULEWC`
- The Carolinas lighting coverage also widened again:
  - `carolinas_lighting_schedule` now covers `OL`, `PL`, `FL`, `YL`, and `GL`
  - old leaf-99 style Carolinas rider-summary pages now have a legacy-total
    fallback path instead of remaining permanently empty
  - the stale `TRAFFIC SIGNAL SERVICE` alias row has been retired once the
    correctly bounded `nc-carolinas-schedule-TS` row was reparsed strongly
- Formula-only skips were also widened for real fixed-formula / posted-value
  riders and programs:
  - `nc-progress-leaf-712`
  - `nc-progress-leaf-721`
  - `nc-progress-leaf-723`
  - `nc-progress-leaf-640`
  - `nc-progress-leaf-663`
- When those families show up again as weak unbounded rows, first verify that
  the latest parse attempt is actually old before adding another profile. The
  queue view can be stale if you inspect it while a reprocess run is still
  in flight.

## If You Need More Detail

- Architecture and data model:
  [architecture.md](/c:/Python/Duke/Standalone/docs/architecture.md)
- Historical pipeline details and limitations:
  [ncuc_pipeline_overview.md](/c:/Python/Duke/Standalone/docs/ncuc_pipeline_overview.md)
- Historical parser profile direction:
  [historical_parser_architecture.md](/c:/Python/Duke/Standalone/docs/historical_parser_architecture.md)
- OCR routing and queue plan:
  [OCR_IMPLEMENTATION_PLAN.md](/c:/Python/Duke/Standalone/docs/OCR_IMPLEMENTATION_PLAN.md)
- Future implementation priorities:
  [roadmap.md](/c:/Python/Duke/Standalone/docs/roadmap.md)
