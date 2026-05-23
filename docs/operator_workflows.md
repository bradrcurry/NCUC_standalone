# Operator Workflows

This doc defines the sanctioned operating workflows for this repo.

Use it to reduce unnecessary repo exploration, ad hoc SQL, and repeated
workflow re-derivation by human operators or AI agents.

These workflows are the default path, not a frozen rulebook. If a workflow is
proven ineffective or incomplete, improve it, update this doc, and promote the
supporting tooling so the next operator inherits the better path.

## Purpose

This file exists to make the project more command-first and less
context-heavy:

- agents should choose a workflow instead of inventing one
- tools should answer recurring operational questions directly
- DB-backed state should replace manual reconstruction from notes and logs
- workflow improvements should be institutionalized instead of rediscovered

Machine-readable companion files:
- [agent_workflows.json](/c:/Python/Duke/Standalone/docs/agent_workflows.json)
- [agent_tool_registry.json](/c:/Python/Duke/Standalone/docs/agent_tool_registry.json)

Human-readable tool policy:
- [agent_tool_use_policy.md](/c:/Python/Duke/Standalone/docs/agent_tool_use_policy.md)

## Operating Principles

- Prefer sanctioned workflows over ad hoc exploration.
- Prefer CLI commands and reusable helpers over manual table inspection.
- Prefer compact summaries and focused next actions over raw dumps.
- Prefer targeted repair and reprocessing over broad reruns.
- If a workflow changes materially, update this file in the same task.
- If a recurring manual step remains necessary, it is a tooling gap.

## Workflow Catalog

### 1. Orient And Check Current State

Use when:
- starting a session
- inheriting work from another agent
- deciding what pipeline state currently needs attention

Read:
- [AGENT_ONBOARDING.md](/c:/Python/Duke/Standalone/AGENT_ONBOARDING.md)
- [NEXT_SESSION_START_HERE.md](/c:/Python/Duke/Standalone/docs/NEXT_SESSION_START_HERE.md)
- [agent_tool_use_policy.md](/c:/Python/Duke/Standalone/docs/agent_tool_use_policy.md)
- [source_of_truth_and_legacy_paths.md](/c:/Python/Duke/Standalone/docs/source_of_truth_and_legacy_paths.md)

Run:

```powershell
python -m duke_rates show-workflow-status-nc
```

If you need detail after the compact summary, follow with:

```powershell
python -m duke_rates parse-review-summary
python -m duke_rates reprocess show-queue-nc
python -m duke_rates reprocess show-stale-historical-nc
python -m duke_rates ocr show-queue-nc
```

To regenerate all DB-driven audit reports (do this at session end to keep reports current):

```powershell
python -m duke_rates export nc-coverage-assessment
python -m duke_rates export nc-anomaly-audit
python -m duke_rates export nc-schedule-inventory-audit
python -m duke_rates export dep-residential-rider-gap-audit
python -m duke_rates export dep-residential-rider-action-queue
python -m duke_rates export dep-compliance-bundle-audit
python -m duke_rates export dep-storm-history-inventory
```

Reports land in `docs/reports/<report-name>/`. The `.md` file in each subfolder is the human-readable summary.
These are the canonical reference for coverage state — trust them over hand-maintained notes.

Outcome:
- know whether the active problem is review backlog, stale stages, OCR, or reprocess backlog
- avoid re-reading dated reports unless a specific investigation requires them

Improvement trigger:
- if a session still needs multiple manual SQL checks to understand current state,
  add or improve a summary command

### 1a. Repair Legacy NCUC State

Use when:
- missing-doc triage fails on legacy enum values such as `portal_harvest`
- historical row loading fails because `current_document_id` contains UUID/text garbage
- an inherited database predates the current sanctioned acquisition-method set

Run:

```powershell
python -m duke_rates lineage repair-legacy-ncuc-data --dry-run
python -m duke_rates lineage repair-legacy-ncuc-data --execute
```

What it repairs:
- rewrites legacy `ncuc_discovery_records.acquisition_method='portal_harvest'` to `playwright`
- clears malformed `historical_documents.current_document_id` text values that cannot be parsed as integers

Outcome:
- `workflow report-nc-missing-doc-triage` and other repository-backed workflows stop failing on these stale rows
- operators get a sanctioned recovery path instead of ad hoc SQL cleanup

### 2. Historical Intake And Mining

Use when:
- new discovery records were downloaded
- PDFs need to enter the historical pipeline

Read:
- [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)
- [ncuc_pipeline_overview.md](/c:/Python/Duke/Standalone/docs/ncuc_pipeline_overview.md)

Run:

```powershell
python -m duke_rates ncuc import-pipeline --all-downloaded
```

For a targeted authenticated-portal harvest that produced a specific manifest or
small set of new discovery rows, do **not** jump straight to `--all-downloaded`.
Use the narrow path instead:

```powershell
python scripts/ingestion/register_harvest_manifest.py --manifest data/<targeted_manifest>.json --dry-run
python scripts/ingestion/register_harvest_manifest.py --manifest data/<targeted_manifest>.json
python -m duke_rates ncuc import-pipeline --record-id <record_id>
```

Use `--record-id` for the freshly registered rows so you do not accidentally pull
the entire historical download backlog into the importer.

**Note:** `ncuc import-pipeline` is the default intake entry point.
`mine-ncuc-pipeline` is retained as a compatibility alias and should not be
stacked on top of the import command in the same workflow. Both routes call
`import_all_pending_downloads()` and already include page-aware span mining.
After intake, run extraction separately:

```powershell
python -m duke_rates extract-rates-nc
```

Current documents (Duke website PDFs) are a separate path — keep them fresh with:

```powershell
python -m duke_rates tariff-update --state NC --company progress --auto-parse
python -m duke_rates tariff-update --state NC --company carolinas --auto-parse
```

Outcome:
- downloaded records become mined page/span evidence and candidate historical rows
- current docs are re-fetched, parsed, and tariff versions updated

Improvement trigger:
- if operators still need to inspect raw downloaded files to decide whether import
  succeeded, add a command-level intake summary

### 2a. Authenticated NCUC Portal Search And Fetch

Use when:
- you need live NCUC portal access
- you want to confirm the NCID/browser path works before harvesting
- you know a docket and want exact docket documents
- you want structured company/date/type search inside the authenticated portal

Do not start with `ncuc public-search` unless you are intentionally doing broad fallback keyword hunting.

Run the commands in this order:

```powershell
python -m duke_rates ncuc portal-smoke-test
python -m duke_rates ncuc portal-search --docket-number "E-2, Sub 1354"
python -m duke_rates ncuc portal-search --company "Duke Energy Progress" --types TARIFF,RATESCED --after 11/01/2025 --before 12/31/2025 --max 20 --top 10
```

Interpretation:
- `ncuc portal-smoke-test` is the preferred health check for the authenticated portal path.
- `ncuc portal-search --docket-number ...` is the preferred exact-docket command.
- `ncuc portal-search` without `--docket-number` is the preferred structured authenticated search.
- `ncuc login-test`, `ncuc resolve-docket-ids`, `ncuc docket-fetch`, and `search doc-param` remain the low-level commands for manual control.

Rules that reduce confusion:
- For `ncuc resolve-docket-ids`, pass docket numbers as `E-2, Sub 1354`.
- For `search doc-param --docket`, pass docket numbers as `E-2 Sub 1354`.
- Always pass `--docket-number` to `ncuc docket-fetch`. Omitting it creates broken metadata downstream.
- A zero-result `search doc-param --docket ...` query does not prove the docket is empty. Prefer `ncuc portal-search --docket-number ...`, which uses exact-docket resolve + inventory instead.

Tested locally on 2026-04-21:
- authenticated login passed
- docket ID resolve for `E-2, Sub 1354` returned the expected exact GUID
- docket fetch dry run for `E-2, Sub 1354` listed 64 documents
- broad authenticated structured search for DEP `TARIFF,RATESCED` filings in November-December 2025 returned 6 documents

### 2a. Manual Historical Slice Registration

Use when:
- you already have a specific PDF on disk
- you know the target family, page bounds, and effective date
- portal discovery found a clean companion or predecessor slice that should go straight into `historical_documents`

Run:

```powershell
python -m duke_rates lineage add-historical-document-nc `
  --family-key nc-progress-leaf-501 `
  --company progress `
  --local-path data/downloads/<file>.pdf `
  --archived-url https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=<guid> `
  --title "Descriptive filing title" `
  --start-page 4 `
  --end-page 6 `
  --effective-start 2013-06-01 `
  --revision-label "Schedule R-TOUD-24A (Leaf No. 501)" `
  --supersedes-label "Schedule R-TOUD-24" `
  --leaf-no 501

python -m duke_rates bootstrap-missing-versions-nc
python -m duke_rates reprocess enqueue-nc --hd-id <historical_document_id> --priority 90
python -m duke_rates reprocess process-queue-nc
```

Notes:
- `lineage add-historical-document-nc` is the sanctioned replacement for the previously documented but missing manual registration step.
- Prefer this command over ad hoc SQL or one-off registration scripts when the PDF/slice is already known.
- Keep `--archived-url` pointed at the regulator/archive source, not the local file path.

Outcome:
- the slice is stored in `historical_documents`
- a `tariff_version` can be bootstrapped and reparsed through the normal queue
- future sessions inherit a reproducible command-first workflow instead of a one-off script

### 2b. Provisional Family Review

Use when:
- the import pipeline has run and created new provisional families
- `lineage list-provisional-families` shows families pending review
- you need to retire garbage auto-key families or promote real ones

Provisional families fall into three categories:

1. **Garbage** — auto-generated from span text fragments (procedural text, addresses,
   certification language, numbered paragraphs). Key longer than ~45 chars is a
   strong signal. Safe to retire if 0 versions and 0 charges.
2. **Duplicate** — title matches a curated family in another state/company. Retire
   after confirming no NC-specific versions link to it.
3. **Real orphan** — a genuine schedule or rider name that has no curated NC family yet.
   Accumulate versions via the import pipeline, then promote via `lineage promote-provisional-family`.

Run:

```powershell
# Bulk-retire all provisional families with no charged content (dry-run first):
python -m duke_rates lineage retire-provisional-garbage-nc --dry-run
python -m duke_rates lineage retire-provisional-garbage-nc --execute

# For individual families with real content that need promotion or single-doc retirement:
python -m duke_rates lineage list-provisional-families --state NC
python -m duke_rates lineage promote-provisional-family FAMILY_KEY
python -m duke_rates lineage retire-historical-document HISTORICAL_DOCUMENT_ID
```

**Caution:** Do not retire provisional families or historical docs while the import
pipeline is still running — the pipeline may be actively writing to those rows.

`lineage retire-provisional-garbage-nc` always preserves families that have at least one charged
tariff version — only zero-charge families are deleted. Run `--dry-run` first to see the
count and re-run `show-workflow-status-nc` after `--execute` to confirm metrics improved.

Outcome:
- garbage provisional families removed in bulk, keeping DB lean
- real schedule/rider families with charges preserved automatically
- `provisional_families`, `null_effective_start`, and `stale_historical` counts all drop

Improvement trigger:
- if provisional family triage still requires manual SQL to distinguish garbage from
  real among charge-bearing families, add a `lineage show-provisional-review-candidates-nc`
  command with auto-scoring

### 3. Lineage And Family-Link Audit

Use when:
- documents or extracted charges appear unlinked
- family assignment looks weak or missing
- provenance needs to move from path-based inference toward durable linkage

Read:
- [architecture.md](/c:/Python/Duke/Standalone/docs/architecture.md)
- [source_of_truth_and_legacy_paths.md](/c:/Python/Duke/Standalone/docs/source_of_truth_and_legacy_paths.md)

Preferred current tools:

```powershell
python -m duke_rates lineage show-gaps-nc
python -m duke_rates lineage validate-nc
python -m duke_rates lineage suggest-family-links-nc --limit 50
python scripts/maintenance/audit_historical_family_mismatches.py
```

Outcome:
- identify strong family-link candidates
- identify contaminated historical assignments
- separate true parser weakness from lineage weakness

Improvement trigger:
- if family-link review still requires one-off SQL or repeated notebook-style
  analysis, promote the audit into a CLI command with compact summary output

### 4. Missing Clean Document Recovery

Use when:
- lineage is known but the clean historical tariff/rider PDF is missing
- gap audits identify weak or deferred clean-document targets
- you want a resumable loop from search through fetch/import/bootstrap/reprocess

Read:
- [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)
- [cli_command_reference.md](/c:/Python/Duke/Standalone/docs/cli_command_reference.md)

Run:

```powershell
python -m duke_rates workflow search-nc-missing-clean-docs --family-key nc-progress-leaf-602
python -m duke_rates workflow run-nc-missing-doc --family-key nc-progress-leaf-602
python -m duke_rates workflow report-nc-missing-doc-triage --family-key nc-progress-leaf-602 --actionable-only --top 10
python -m duke_rates workflow execute-top-nc-missing-doc-triage --family-key nc-progress-leaf-602
python -m duke_rates workflow execute-batch-nc-missing-doc-triage --family-key nc-progress-leaf-602 --max-actions 3
python -m duke_rates workflow show-nc-missing-doc-status --family-key nc-progress-leaf-602
python -m duke_rates workflow report-nc-missing-doc-deferred
python -m duke_rates workflow plan-nc-missing-doc-remediation
```

When a deferred reason has an implemented repair path, use:

```powershell
python -m duke_rates workflow remediate-nc-missing-doc-no-download-url
python -m duke_rates workflow remediate-nc-missing-doc-effective-start
python -m duke_rates workflow remediate-nc-missing-doc-confidence
python -m duke_rates workflow remediate-and-promote-nc-missing-docs
python -m duke_rates workflow promote-nc-missing-doc-targets
```

Notes:
- `workflow run-nc-missing-doc` is the sanctioned end-to-end loop. It can resume
  from intermediate stages instead of forcing a restart.
- `workflow search-nc-missing-clean-docs` now applies generic search escalation for
  difficult targets. It starts with exact docket structured queries, expands to
  nearby docket variants, then falls back to a docketless broad structured
  search before relying on richer keyword fan-out.
- The search fan-out also uses broader family clues such as schedule code,
  title phrases, leaf references, redline hints, and multiple docket hints. Do
  not assume one failed exact docket query means the portal path is exhausted.
- Portal docket resolution is no longer exact-text only. Near matches can be
  returned with `normalized_exact`, `same_base_and_sub`, or `partial`
  `match_type` values so the workflow can continue without brittle label
  formatting assumptions.
- `workflow report-nc-missing-doc-triage` is the preferred queue surface for weaker
  agents. It reads persisted `next_action` / `blocked_reason` metadata from prior
  validate passes, ranks actionable targets, and prints the sanctioned command
  for each one.
- `workflow execute-top-nc-missing-doc-triage` is the preferred bounded auto-advance path
  when the top actionable target should be executed directly instead of only
  suggested.
- `workflow execute-batch-nc-missing-doc-triage` is the bounded multi-step path. Use a
  conservative `--max-actions` and rely on its stop conditions instead of
  open-ended loops.
- Use `workflow show-nc-missing-doc-status` to inspect one family/target before doing
  manual intervention.
- Treat `workflow report-nc-missing-doc-deferred` as the queue of still-blocked targets,
  not as a final failure state.

Triage action mapping:
- `fetch_document`: run the workflow fetch stage for the targeted discovery row.
- `retry_fetch_or_manual_portal_review`: rerun the fetch stage with failed-fetch retry enabled.
- `import_and_mine_document`: run the workflow import stage for the targeted discovery row.
- `bootstrap_tariff_version`: run the workflow bootstrap stage for the targeted historical doc.
- `process_document` or `retry_with_better_parser_context`: run the workflow from `queue_reprocess` through `process_reprocess`.
- `review_family_assignment`, `review_parse_output`, `ready_for_acceptance`, `wait_for_reprocess_completion`, `monitor_linked_document`: inspect with `workflow show-nc-missing-doc-status` before taking any manual action.

Outcome:
- missing clean-document candidates are searched, fetched, imported, versioned,
  and queued for parsing through one reproducible workflow
- deferred targets are grouped by repairable reason instead of disappearing into notes
- remediation work stays command-first and reviewable

Improvement trigger:
- if operators still need ad hoc SQL or handwritten notes to track why a
  missing-doc target stalled, extend the remediation/status commands

### 5. Extraction And Validation

Use when:
- historical documents and tariff versions are already linked
- the next step is charge extraction and validation

Read:
- [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)
- [historical_parser_architecture.md](/c:/Python/Duke/Standalone/docs/historical_parser_architecture.md)

**Prerequisite:** `extract-rates-nc` only processes documents where
`historical_documents.effective_start IS NOT NULL` AND `tariff_versions.historical_document_id`
links to the doc. Run `ncuc import-pipeline` first (which mines span dates), then bootstrap
any docs that still lack version links:

```powershell
python -m duke_rates ncuc import-pipeline --all-downloaded
python -m duke_rates bootstrap-missing-versions-nc
```

For current documents, ensure they are parsed before extraction:

```powershell
python -m duke_rates parse-batch --state NC --company progress
python -m duke_rates parse-batch --state NC --company carolinas
```

Then run extraction and review:

```powershell
python -m duke_rates extract-rates-nc
python -m duke_rates parse-review-summary
python -m duke_rates show-parser-selection-audit-nc --limit 25
python -m duke_rates show-workflow-next-actions-nc --limit 10
python -m duke_rates show-workflow-capabilities-nc
python -m duke_rates show-workflow-action-receipts-nc --limit 10
python -m duke_rates reconcile-workflow-action-receipts-nc --limit 10
python -m duke_rates ocr show-remediation-candidates-nc --limit 25
python -m duke_rates ocr enqueue-remediation-nc --limit 10          # dry-run by default
python -m duke_rates ocr enqueue-remediation-nc --limit 10 --execute
python -m duke_rates ocr process-backlog-nc --workers 4             # full workflow: enqueue -> drain -> extract
```

The canonical OCR backlog workflow is `ocr process-backlog-nc`. It wraps the
three-step sequence (remediation enqueue, queue drain via `--until-empty`, rate
extraction) in one command. Use the individual commands only when you need to
inspect intermediate state or operate on a subset. For structure-sensitive
documents (the `run_docling_or_paddle_structure` lane), follow the super-command
with `process-docling-batch --ocr-remediation --source historical`.

For a focused diagnostic pass on one family:

```powershell
python -m duke_rates extract-rates-nc --family-key nc-progress-leaf-500 --verbose
```

`--verbose` prints status buckets plus zero-charge and failed-document details, so
operators can tell whether a family is blocked by missing version links, empty
parser output, or hard extraction failures.

Outcome:
- extraction runs only on linked historical versions with effective dates
- weak results are surfaced through the review queue instead of being lost
- `parse-review-summary` shows legacy `tiered_ingest` backlog separately from
  new span-pipeline profiles — do not confuse the two
- `show-parser-selection-audit-nc` highlights whether weak/empty latest runs are
  concentrated in one profile or one initial->fallback transition
- `show-document-classification-audit-nc` should be the next check when the
  question is whether the corpus needs better parser routing versus a different
  document lane entirely such as formula/reference/redline/unrelated-but-keep
- `needs_normalization` rows in the classification audit are not parser-profile
  work yet; route them through `ocr show-remediation-candidates-nc` or
  `ocr enqueue-remediation-nc` so OCR/Paddle/Docling can recover usable text
  before profile selection is re-evaluated
- `needs_processing` rows have usable text but no latest historical processing
  run; queue the concrete `--hd-id` values suggested by
  `show-parser-improvement-candidates-nc` before writing parser-profile code
- `show-unknown-routing-audit-nc` should follow when you want a ranked family
  list for targeted parser work instead of another broad sweep across all
  `unknown` rows
- `show-parser-improvement-candidates-nc` is the compressed handoff surface for
  weaker agents: it ranks family-level parser/routing work and prints the next
  sanctioned command to run for each family
- `reprocess enqueue-parser-improvement-nc` drains the easy-win cohort
  (`recommended_action=enqueue_reprocess`) without per-family hd-id wrangling.
  These are documents flagged with usable text plus a working profile but no
  latest pipeline run — pure operational debt, no parser changes required.
  Defaults to `--dry-run`; add `--execute` to enqueue and `--process` to drain
  the reprocess queue in the same invocation.
- `ocr show-remediation-candidates-nc` is the preferred first OCR triage surface
  for `unknown` / no-text cohorts; it recommends either the lighter OCR lane or
  Docling/layout-heavy escalation

OCR triage note:
- for weaker agents, `show-workflow-next-actions-nc` is the preferred first
  surface because it compresses OCR, reprocess, and parser next steps into one
  ranked list
- `show-workflow-capabilities-nc` is the policy surface for sanctioned
  concurrency:
  `ocr process-queue-nc` and `reprocess process-queue-nc` are local-only
  `workers_allowed`; portal/search and queue-enqueue actions remain
  `sequential_only`
- `execute-workflow-next-action-nc --limit N --workers M` now honors that
  policy automatically: local queue actions can use bounded workers, while
  sequential-only actions ignore worker scaling
- `show-workflow-action-receipts-nc` is the recovery surface after a timeout or
  interrupted guided run; a `started` receipt means the action was launched even
  if the shell session did not stay alive long enough to print completion
- `show-workflow-action-receipts-nc` now reconciles receipts by default, and
  `reconcile-workflow-action-receipts-nc` is the explicit repair command if you
  want to refresh receipt status first
- use `ocr show-remediation-candidates-nc` first when the problem is
  `unknown + no text` or repeated weak/empty parsing
- use `ocr enqueue-remediation-nc` to move only the lighter
  `queue_ocr_or_paddle` cohort into the OCR queue
- use `ocr report-benchmark-nc` only when OCR artifacts already exist and you
  want to compare backend/outcome cohorts

**Note on parse-review-summary:** The large `tiered_ingest / unknown` backlog
(~3,300 needs_review) is legacy pipeline output and does not block current
extraction. Focus reviews on the new span-pipeline profiles only.

Improvement trigger:
- if validation still depends on manually opening multiple scripts or logs,
  add a compact validation rollup command

### 6. Targeted Reprocessing

Use when:
- parser logic changed
- stale artifacts exist
- weak parses need focused remediation
- OCR/docling or profile-specific changes should affect a bounded set of rows

Read:
- [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)
- [historical_parser_architecture.md](/c:/Python/Duke/Standalone/docs/historical_parser_architecture.md)

Run:

```powershell
python -m duke_rates reprocess enqueue-nc --from-needs-review
python -m duke_rates reprocess enqueue-nc --hd-id 401 --hd-id 2595
python -m duke_rates reprocess show-queue-nc
python -m duke_rates reprocess show-priority-nc
python -m duke_rates reprocess process-queue-nc
python -m duke_rates reprocess show-profile-impact-nc --parser-profile progress_residential_tou
python -m duke_rates reprocess enqueue-profile-impact-nc --parser-profile progress_residential_tou
```

Notes:
- `reprocess enqueue-nc` no longer pulls the needs-review backlog by default.
- Use `--from-needs-review` for backlog-driven reparsing.
- Use `--hd-id` for surgical reruns after a parser/profile fix.

Outcome:
- reruns stay selective and explainable
- changes propagate through persisted queue state instead of broad rescans

Improvement trigger:
- if operators need to manually decide queue priority from raw tables, improve
  queue ranking commands

### 7. Provenance And Fingerprint Audit

Use when:
- you need to know which rows are safe to reuse or backfill
- document identity depends too heavily on paths
- provenance columns are incomplete

Read:
- [architecture.md](/c:/Python/Duke/Standalone/docs/architecture.md)
- [source_of_truth_and_legacy_paths.md](/c:/Python/Duke/Standalone/docs/source_of_truth_and_legacy_paths.md)

Current practical surface:
- `lineage show-provenance-gaps-nc`
- `lineage show-fingerprint-coverage-nc`
- `document_fingerprints`
- `parse_attempt_logs`
- `parse_review_outcomes`
- `historical_processing_runs`
- `historical_reprocess_queue`

Desired long-term CLI replacements:
- `backfill-historical-provenance-nc`

Outcome:
- understand which rows can be reused, reprocessed, or backfilled safely
- reduce dependence on session memory to understand processing stage history

Improvement trigger:
- if an agent still has to reconstruct stage/provenance status from several
  tables, add a summary command

### 7. Workflow And Tooling Improvement

Use when:
- a sanctioned workflow is too expensive, unclear, or ineffective
- a repeated investigation pattern appears across sessions
- an agent keeps reading docs or writing SQL for the same question

Do this:

1. identify the repeated manual step
2. build or improve a local tool for it
3. document the sanctioned usage here
4. update the nearest canonical workflow doc
5. update [scripts/README.md](/c:/Python/Duke/Standalone/scripts/README.md) if a reusable helper was added
6. update [tool_workflow_backlog.md](/c:/Python/Duke/Standalone/docs/tool_workflow_backlog.md) if a tooling gap remains open after the task

Promotion rule:
- if a manual step is repeated across more than one meaningful task, treat it
  as a tooling candidate

Outcome:
- project knowledge moves out of transient reasoning and into reusable local
  capability

## Change Policy

Sanctioned workflows should evolve under evidence, not preference.

Change a workflow when:
- the current path is producing repeated operator confusion
- a local tool can replace repeated manual reasoning
- the workflow is forcing broad reruns where targeted work is possible
- the workflow depends on stale docs or missing state surfaces

When changing a workflow:

1. update this file
2. update the narrower domain doc it relies on
3. update [AGENT_ONBOARDING.md](/c:/Python/Duke/Standalone/AGENT_ONBOARDING.md) only if the entry path changed
4. add or update tests if code behavior changed
5. record evidence in a dated report only if the evidence itself matters later

## Tooling Priorities For Token Reduction

These are high-leverage additions because they turn repeated reasoning tasks
into direct operator commands.

### Promotion Candidates From Existing Scripts

- [audit_stranded_ncuc_family_clues.py](/c:/Python/Duke/Standalone/scripts/maintenance/audit_stranded_ncuc_family_clues.py)
- [audit_historical_family_mismatches.py](/c:/Python/Duke/Standalone/scripts/maintenance/audit_historical_family_mismatches.py)
- [check_new_charges.py](/c:/Python/Duke/Standalone/scripts/debug/check_new_charges.py)
- [final_charge_summary.py](/c:/Python/Duke/Standalone/scripts/debug/final_charge_summary.py)

Those helpers are useful, but the long-term target is a stable CLI-first
operator surface rather than relying on a growing pile of scripts.

## Known Tool Behaviors and Traps

These are non-obvious behaviors discovered during backlog processing that can waste time if rediscovered.

### Parser Profile Registry: Dual Scoring System

Every parser profile has TWO independent registration points that must both be updated:

1. **Profile class** (`parser_profiles.py`): `_SUPPORTED_FAMILIES` set + `supports()` method
2. **Static scoring function** (`parser_profiles.py`): `_score_profile()` — called by `rank_candidates()`, completely bypasses the class methods

The registry's `rank_candidates()` calls the static `_score_profile()` function exclusively. Updating only the class methods has zero effect on profile selection. Both locations must be kept in sync.

**When adding or modifying a profile**, search for `_score_profile` in `parser_profiles.py` and add/update the corresponding case. The function is defined near the registry class (not within it) and uses `if profile.name == “profile_name”` branches.

### Profile Impact CLI: Stale Profile List

`reprocess show-profile-impact-nc --parser-profile <name>` validates against `_PROFILE_IMPACT_RULES` in `profile_dependencies.py`. Profiles added to `parser_profiles.py` but not listed here will be rejected as “Unknown parser profile.”

**When adding a profile**, add a `ParserProfileImpactRule` entry to `_PROFILE_IMPACT_RULES` in `profile_dependencies.py`.

### OCR Normalization Stage: No Auto-Advance

Documents in stage `ocr_normalization_version` complete OCR normalization but do NOT auto-advance to extraction. They sit in a “stale” state requiring manual reprocess enqueue. This is why `stale_historical` can contain docs that were OCR'd but not extracted.

**Workaround:** Run `reprocess enqueue-stale-nc` regularly after OCR remediation batches to advance normalized docs through extraction.

### Enqueue Reprocess: --family-key Is Not Standalone

`reprocess enqueue-nc --family-key X` does NOT enqueue all docs for family X. The `--family-key` flag is only a filter modifier for `--from-needs-review`. To extract a specific family directly, use `extract-rates-nc --family-key X`.

### Extraction Family Filter: OR Semantics

`extract-rates-nc --family-key A --family-key B` processes documents matching ANY of the specified family keys (OR logic), not documents that match all of them. The output shows “Processing N historical documents” across all matching families.

### Parser Score Profile: Case Sensitivity

The `_score_profile()` function checks `profile.name` against lowercased strings. When adding a new case, the comparison must match the `name` field of the profile class exactly (typically lowercase with underscores).

## Anti-Patterns

Avoid:

- asking agents to infer the workflow from dated reports
- leaving repeated SQL snippets as the only way to inspect system state
- adding new tooling without documenting where it fits in the workflow catalog
- letting “sanctioned” become “frozen” when evidence shows the workflow should improve
- expanding onboarding instead of updating the local tools
