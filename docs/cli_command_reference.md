# CLI Command Reference

**Purpose:** Concise reference guide for all `duke-rates` CLI commands organized by functional
area. Intended for AI agents and operators who need to find the right command quickly without
reading `cli.py` or running `--help` on every command.

**Invocation:** `python -m duke_rates <command> [options]`  
**Entry point:** `duke-rates` (same thing, requires `pip install -e .`)  
**Canonical source:** `src/duke_rates/cli.py`
**Supported default path:** `docs/agent_tool_registry.json` + `docs/agent_workflows.json`

This file remains the broad command reference. For new agent sessions, prefer the
machine-readable manifests first so supported tools, compatibility aliases, and
legacy paths are not treated as equivalent.

## How Agents Should Use This File

Use this document as a human-readable map of the CLI surface, not as the only
source of workflow truth.

Agent operating rules:

1. Start with `docs/agent_tool_registry.json` and `docs/agent_workflows.json`.
   Use this file to understand the broader surface and to find the right command
   family when the machine-readable manifests are not enough.
2. Prefer the commands explicitly labeled as primary, supported, or preferred.
   Treat compatibility aliases and legacy paths as valid only when a workflow or
   older note explicitly requires them.
3. Prefer session-orientation commands before mutation commands. In practice,
   start with `show-workflow-status-nc`, then inspect the queue or audit surface
   that matches the current bottleneck.
4. Prefer bounded repair workflows over broad reruns. If a problem can be solved
   with `--record-id`, `--hd-id`, family-specific search, targeted reprocess, or
   a remediation command, use that before `--all-downloaded` or other large sweeps.
5. When the problem is document acquisition or lineage closure, use the
   missing-document workflow commands. Do not jump straight to parser work if the
   clean source document is still missing or provenance is weak.
6. When this file conflicts with `src/duke_rates/cli.py`, the CLI source wins.
   Update this file in the same task when a command is added, renamed, or materially
   changes behavior.

## Default Decision Path

If you are unsure which command family to use, choose in this order:

1. `show-workflow-status-nc`
2. If the problem is weak parses or reruns: `parse-review-summary`, `reprocess show-queue-nc`, `reprocess show-priority-nc`
3. If the problem is stale artifacts: `reprocess show-stale-historical-nc`
4. If the problem is lineage or version linkage: `show-lineage-gaps-nc`, `validate-lineage-nc`
5. If the problem is document identity, routing, or reuse confidence: `show-provenance-gaps-nc`, `show-fingerprint-coverage-nc`, `show-document-classification-audit-nc`
6. If the problem is missing clean historical PDFs: `workflow search-nc-missing-clean-docs`, `workflow run-nc-missing-doc`
7. If the problem is new downloaded NCUC records: `ncuc-import-pipeline`, then `bootstrap-missing-versions-nc`, then `extract-rates-nc`

## Source-Of-Truth Docs

Read these before inventing a new workflow:

- [AGENT_ONBOARDING.md](/c:/Python/Duke/Standalone/AGENT_ONBOARDING.md)
- [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md)
- [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)
- [agent_tool_registry.json](/c:/Python/Duke/Standalone/docs/agent_tool_registry.json)
- [agent_workflows.json](/c:/Python/Duke/Standalone/docs/agent_workflows.json)
- [source_of_truth_and_legacy_paths.md](/c:/Python/Duke/Standalone/docs/source_of_truth_and_legacy_paths.md)

---

## Sub-Apps (Typer Sub-Commands)

As of 2026-05-12, some commands are being reorganized into Typer sub-apps to make the CLI surface easier to navigate and to enable GitNexus indexing of the sub-app modules. The first migration is the `ocr` group.

| Sub-app | Invocation | Status | Module |
|---|---|---|---|
| `ocr` | `python -m duke_rates ocr <command>` | ✅ Active | `src/duke_rates/cli_commands/ocr.py` |

Run `python -m duke_rates <subapp> --help` to list the commands in a sub-app. The full refactor plan is in [CLI_REFACTOR_PLAN.md](/c:/Python/Duke/Standalone/docs/CLI_REFACTOR_PLAN.md); future phases will add `doc-intel`, `ncuc`, `lineage`, `export`, `billing`, `reprocess`, `workflow`, `progress`, and `search` sub-apps.

## Quick Orientation Commands

Run these at the start of any session to understand current pipeline state:

```bash
python -m duke_rates show-workflow-status-nc      # compact NC workflow summary
python -m duke_rates parse-review-summary          # review backlog overview (legacy vs new pipeline)
python -m duke_rates reprocess show-queue-nc       # pending reprocess jobs
python -m duke_rates reprocess show-stale-historical-nc      # historical docs with stale or missing stage
python -m duke_rates ocr show-queue-nc             # OCR queue depth (sub-app)
python -m duke_rates list-provisional-families --state NC   # provisional families needing review
```

For document intelligence state (classification, embeddings, LLM):
```bash
python -m duke_rates check-ollama-models-nc                     # Ollama model availability
python -m duke_rates list-document-types-nc                     # taxonomy
python -m duke_rates report-document-types-nc                   # classification distribution
python -m duke_rates report-classification-disagreements-nc --cross-stage document_type  # rule vs embedding
python -m duke_rates report-flag-classifications-nc             # flag classifier audit
```

Do not skip this orientation step unless you already know the exact bounded task
you are performing.

---

## 1. Current Document Pipeline (Duke Website)

Commands that manage the live-site tariff documents (non-historical).

| Command | What it does |
|---|---|
| `tariff-update` | Fetch latest tariffs from Duke's website, deduplicate by rev_token, optionally parse |
| `crawl` | Crawl Duke's website to discover tariff document links |
| `parse` | Parse a single document |
| `parse-batch` | Parse a batch of documents by state/company |
| `classify-docs` | Classify documents by type (tariff sheet, rider, etc.) |
| `show-doc` | Show metadata and parsed state for a single document |
| `list-docs` | List documents with optional filters |
| `attach-current-document-to-family` | Link a current document to a tariff family |
| `sync-family-metadata-from-current-anchor` | Pull metadata from the current anchor into the family record |
| `list-current-anchor-mismatches` | Show families where the current anchor doesn't match expected |
| `repair-historical-current-snapshot` | Repair a historical document's link to its current snapshot |
| `repair-legacy-ncuc-data` | Audit and repair legacy NCUC rows that break modern workflow tooling |
| `show-lineage-gaps-nc` | Summarize NC lineage gaps across unlinked discovery records, historical docs, versions, and no-charge families |
| `suggest-family-links-nc` | Suggest likely family assignments for stranded NC discovery records using span clues; optional `--apply` persists them |

**Typical workflow:**
```bash
python -m duke_rates tariff-update --state NC --company progress --auto-parse
python -m duke_rates tariff-update --state NC --company carolinas --auto-parse
```

---

## 2. NCUC Historical Pipeline

### 2a. Discovery and Download

| Command | What it does |
|---|---|
| `ncuc-seed-discover` | Seed the discovery queue with known NCUC docket anchors |
| `ncuc-search` | Run a targeted NCUC portal text search |
| `ncuc-smart-search` | Run a search with quality-filtered results (preferred over raw search) |
| `ncuc-public-search` | Search the NCUC public filing index |
| `ncuc-annual-orders-scan` | Scan annual NCUC order index for tariff filings |
| `ncuc-docket-fetch` | Fetch all documents from a specific docket number |
| `ncuc-resolve-docket-ids` | Resolve raw docket IDs to canonical NCUC document IDs |
| `ncuc-playwright-discover` | Playwright-based portal scrape (requires Chrome + NCID auth) |
| `ncuc-portal-scrape` | Low-level portal scrape |
| `ncuc-wayback-discover` | Search Wayback Machine for archived Duke tariff URLs |
| `ncuc-wayback-harvest` | Download and register found Wayback Machine documents |
| `ncuc-fetch` | Fetch a document by URL and register it |
| `ncuc-fetch-portal` | Fetch directly from NCUC portal URL |
| `ncuc-ingest-url` | Ingest a single URL into the discovery queue |
| `ncuc-list` | List discovery records with status filters |
| `ncuc-show` | Show full detail on a discovery record |
| `ncuc-pending-rates` | List discovery records pending rate extraction |
| `ncuc-login-test` | Low-level auth probe for authenticated DocketDetails access |
| `ncuc-portal-smoke-test` | Canonical authenticated portal smoke test: login, resolve, DocketDetails, docket inventory |
| `ncuc-portal-search` | Canonical authenticated portal search: exact-docket with `--docket-number`, structured search otherwise |
| `ncuc-family-query` | Query discovery records for a specific family key |

**Authenticated portal workflow is canonical.** Use the public search only as a fallback when you are deliberately doing broad keyword hunting.

**Tested on 2026-04-21:**
- `python -m duke_rates ncuc-login-test` succeeded with Chrome and authenticated DocketDetails access.
- `python -m duke_rates ncuc-resolve-docket-ids --docket-number "E-2, Sub 1354"` returned the expected exact match.
- `python -m duke_rates ncuc-docket-fetch 9b3614b6-11d6-4703-8d18-5e2e2ef3d705 --docket-number "E-2, Sub 1354" --dry-run` listed 64 docket documents.
- `python -m duke_rates search doc-param --company "Duke Energy Progress" --types TARIFF,RATESCED --after 11/01/2025 --before 12/31/2025 --max 20 --top 10` returned 6 results.

**Do not treat these commands as interchangeable:**
- `ncuc-portal-smoke-test` is the preferred first check. It verifies login, resolve, DocketDetails access, and docket inventory in one run.
- `ncuc-portal-search` is the preferred authenticated search surface.
- `ncuc-login-test`, `search doc-param`, `ncuc-resolve-docket-ids`, and `ncuc-docket-fetch` remain lower-level commands for manual control.
- `ncuc-public-search` is the weaker public fallback. Do not start there for normal portal work.

**Canonical command sequence:**
```powershell
python -m duke_rates ncuc-portal-smoke-test
python -m duke_rates ncuc-portal-search --docket-number "E-2, Sub 1354"
python -m duke_rates ncuc-portal-search --company "Duke Energy Progress" --types TARIFF,RATESCED --after 11/01/2025 --before 12/31/2025 --max 20 --top 10
```

**Important limitation:** a zero-result `search doc-param --docket ...` query does not prove the docket is empty. Prefer `ncuc-portal-search --docket-number ...`, which uses exact-docket resolve + inventory instead of the brittle structured-docket path.

### 2b. Import and Mining

| Command | What it does |
|---|---|
| `ncuc-import-pipeline` | **Primary intake command.** Import all pending downloads: mines page/span evidence, assigns family keys, creates provisional families. Alias: `mine-ncuc-pipeline`. Do NOT run both simultaneously. |
| `add-historical-document-nc` | Register one page-bounded NC historical PDF directly into `historical_documents` when you already know the correct family/date/page range |
| `rebind-historical-page-range` | Update the bounded page range for an existing historical row and optionally requeue it |
| `mine-tariff-sheets-nc` | Mine tariff sheets specifically (narrower than full pipeline) |
| `ncuc-mine-pdf-content` | Mine PDF content for a specific document |
| `ncuc-import-exhibit-candidates` | Import candidate exhibit documents from NCUC filings |
| `ncuc-list-exhibit-candidates` | List candidate exhibits pending import decision |
| `bootstrap-missing-versions-nc` | Create minimal `tariff_version` rows for historical docs that have `effective_start` + `local_path` but no version link. Run after `ncuc-import-pipeline`. |
| `load-ncuc-ingest` | Load raw NCUC ingest records from a file |
| `import-history-inbox-progress-nc` | Import documents from the history inbox |
| `export-history-inbox-progress-nc` | Export history inbox contents |

**Critical note:** `extract-rates-nc` requires both `effective_start IS NOT NULL` and a
`tariff_versions.historical_document_id` link. Run `ncuc-import-pipeline` then
`bootstrap-missing-versions-nc` before extraction.

### 2c. Missing-document recovery

| Command | What it does |
|---|---|
| `workflow search-nc-missing-clean-docs` | Search NCUC for likely clean historical tariff/rider documents tied to known family gaps |
| `workflow run-nc-missing-doc` | Run the resumable end-to-end missing-document loop: search, fetch, import, bootstrap, and queue |
| `workflow promote-nc-missing-doc-targets` | Re-evaluate already-found missing-doc targets and advance only the rows that now qualify |
| `workflow show-nc-missing-doc-status` | Show workflow/provenance state for one missing-document family or target |
| `workflow report-nc-missing-doc-triage` | Show persisted next-action / blocker triage for resumable missing-document work; supports ranked actionable output and exact next-command suggestions |
| `workflow execute-top-nc-missing-doc-triage` | Execute the top ranked actionable triage target through its sanctioned underlying workflow |
| `workflow execute-batch-nc-missing-doc-triage` | Execute up to `N` ranked actionable triage targets with guarded stop conditions |
| `workflow report-nc-missing-doc-deferred` | Summarize deferred missing-document targets by blocking reason |
| `workflow plan-nc-missing-doc-remediation` | Rank supported remediation actions by likely payoff and frequency |
| `workflow report-nc-missing-doc-remediation-history` | Show persisted remediation execution history |
| `workflow execute-top-nc-missing-doc-remediation` | Execute the highest-ranked supported missing-doc remediation step |
| `workflow remediate-nc-missing-doc-no-download-url` | Re-open deferred discovery rows blocked on missing download URLs and try to recover them |
| `workflow remediate-nc-missing-doc-effective-start` | Re-open imported historical docs blocked on missing `effective_start` and try to recover dates |
| `workflow remediate-nc-missing-doc-confidence` | Broaden search for targets deferred on confidence/ideality thresholds |
| `workflow remediate-and-promote-nc-missing-docs` | Run supported remediations and immediately promote rows that become eligible |

**Typical missing-document sequence:**
```bash
python -m duke_rates workflow search-nc-missing-clean-docs --family-key nc-progress-leaf-602
python -m duke_rates workflow run-nc-missing-doc --family-key nc-progress-leaf-602
python -m duke_rates workflow report-nc-missing-doc-triage --family-key nc-progress-leaf-602 --actionable-only --top 10
python -m duke_rates workflow execute-top-nc-missing-doc-triage --family-key nc-progress-leaf-602
python -m duke_rates workflow execute-batch-nc-missing-doc-triage --family-key nc-progress-leaf-602 --max-actions 3
python -m duke_rates workflow show-nc-missing-doc-status --family-key nc-progress-leaf-602
python -m duke_rates workflow report-nc-missing-doc-deferred
python -m duke_rates workflow remediate-and-promote-nc-missing-docs
```

Use this loop when the main problem is document acquisition and lineage closure,
not parser behavior. The workflow is resumable and is the preferred replacement
for handwritten gap-tracking notes.

Generic hard-case search behavior:
- `workflow search-nc-missing-clean-docs` no longer relies on only one exact docket form.
  It escalates through exact docket search, nearby docket expansion, and a
  docketless broad structured search when the direct query is weak.
- Keyword search now fans out across richer family clues such as schedule code,
  title phrases, leaf references, redline hints, and more than one docket hint
  instead of anchoring on a single docket string.
- Portal docket resolution is tolerant of formatting variation. Near matches can
  now be returned as `normalized_exact`, `same_base_and_sub`, or `partial`
  instead of failing closed on exact visible-text mismatch.
- When the direct portal path is noisy, prefer the sanctioned triage and
  remediation surfaces over inventing speculative one-off docket hunts.

Use `workflow report-nc-missing-doc-triage --actionable-only --top 10` when an agent needs
the narrowest ready-made work queue. It ranks persisted `next_action` /
`blocked_reason` metadata from earlier validate passes and prints the exact
sanctioned command for each target, so weaker agents can resume from explicit
guidance instead of recomputing target triage from scratch.

Use `workflow execute-top-nc-missing-doc-triage` when you want the tool to take the next
sanctioned step automatically for the highest-ranked actionable target instead of
just suggesting the command.

Use `workflow execute-batch-nc-missing-doc-triage` for bounded automation across multiple
targets. It stops when it hits `--max-actions`, finds no more actionable targets,
detects a repeated top target, or sees no ranked-queue progress after a step.

**Triage decision table:**

| `next_action` | Meaning | Preferred command | Mutates state |
|---|---|---|---|
| `fetch_document` | Discovery row is ready to fetch | `workflow run-nc-missing-doc --from-stage fetch --to-stage fetch ...` | Yes |
| `retry_fetch_or_manual_portal_review` | Discovery fetch failed or needs another fetch attempt | `workflow run-nc-missing-doc --from-stage fetch --to-stage fetch --retry-failed-fetch ...` | Yes |
| `import_and_mine_document` | Downloaded discovery row should be imported into historical/span state | `workflow run-nc-missing-doc --from-stage import --to-stage import ...` | Yes |
| `bootstrap_tariff_version` | Historical doc exists but lacks a usable `tariff_version` link | `workflow run-nc-missing-doc --from-stage bootstrap_versions --to-stage bootstrap_versions ...` | Yes |
| `process_document` | Historical doc is ready for queue/process execution | `workflow run-nc-missing-doc --from-stage queue_reprocess --to-stage process_reprocess ...` | Yes |
| `retry_with_better_parser_context` | Historical doc parsed weakly/empty and should be rerun through the bounded reprocess path | `workflow run-nc-missing-doc --from-stage queue_reprocess --to-stage process_reprocess ...` | Yes |
| `review_family_assignment` | Family assignment still needs inspection before mutation | `workflow show-nc-missing-doc-status ...` | No |
| `review_parse_output` | Parse output needs review before accepting or promoting further | `workflow show-nc-missing-doc-status ...` | No |
| `ready_for_acceptance` | Target looks stable; inspect before any manual acceptance/review action | `workflow show-nc-missing-doc-status ...` | No |
| `wait_for_reprocess_completion` | Work is already queued/running; inspect only | `workflow show-nc-missing-doc-status ...` | No |
| `monitor_linked_document` | Target is linked and not currently actionable | `workflow show-nc-missing-doc-status ...` | No |

The triage executor commands use this same mapping internally. Prefer the queue
surface over inventing your own command mapping.

### 2d. Extraction

| Command | What it does |
|---|---|
| `extract-rates-nc` | **Primary extraction command.** Run all parser profiles against linked historical documents. Produces `tariff_charges` rows. Documents flagged `is_redline_candidate=1` in `document_fingerprints` are skipped early with outcome `skipped_redline`. Float-conversion errors (e.g. OCR-malformed `'1.631.88'`) are caught per-profile, recorded as `parse_warnings` in run metadata, and let the fallback chain continue instead of failing the whole document. Each run also persists `top_candidates` (top 5 ranked profiles + scores + reasons) so `unknown` outcomes show what almost matched. A family-vs-content mismatch heuristic detects rider docs whose page-bounded slice landed on a different schedule's text and classifies them as `skipped_reference` (currently covers EDPR, BPMPROSPECTIVERIDER, BPMPPTTRUEUP). Schedule NL and HP are classified as `skipped_formula` due to per-customer formula structure. Schedule S extracts via the `carolinas_residential_flat` profile. Pass `--progress` for a periodic stderr status line during long runs (`--progress-interval N` controls cadence, default 30s). |
| `validate-extraction-nc` | Validate extraction results for anomalies |
| `reprocess show-profile-impact-nc` | Show which documents would be affected by a specific parser profile |
| `reprocess enqueue-profile-impact-nc` | Enqueue reprocessing for documents matching a parser profile |
| `parse-review-summary` | Summary of parse review backlog with top profiles, families, and outstanding needs-review root causes (shows legacy `tiered_ingest` separately) |
| `show-parser-selection-audit-nc` | Fast audit of latest parser-profile selection outcomes: generic-profile reliance, fallback transitions, and weak/empty latest runs |
| `show-parser-improvement-candidates-nc` | Merge parser-selection and routing signals into one ranked family-level parser-improvement queue with a suggested next command |
| `show-near-miss-profiles-nc` | Surfaces the `top_candidates` diagnostic from problem runs (empty/weak/missing). Aggregates by (a) top near-miss profile — fix or extend these to convert near-miss → parsed, (b) families with no near-miss — need a new profile family, (c) profiles emitting `parse_warnings` — harden float coercion. Use `--min-score N` to filter to higher-confidence near-misses, `--company` to scope, `--json` for machine-readable output |
| `show-workflow-status-nc` | Compact NC orientation summary: review, reprocess, stale, OCR, coverage, provisional-family, and null-date counts |
| `show-workflow-next-actions-nc` | Rank the next bounded NC workflow actions across OCR, queue, stale, and parser-audit surfaces, including concurrency policy and local parallel-safe examples |
| `execute-workflow-next-action-nc` | Execute the highest-priority bounded executable workflow step; honors sanctioned worker policy for local queue actions via `--workers` / `--auto-workers` |
| `show-workflow-capabilities-nc` | Show the sanctioned concurrency policy for guided NC workflow actions, including which ones are `workers_allowed` versus `sequential_only` |
| `show-workflow-action-receipts-nc` | Show durable receipts for guided workflow actions so interrupted or timed-out runs can be resumed or audited |
| `reconcile-workflow-action-receipts-nc` | Reconcile guided workflow receipts against OCR/reprocess queue state and promote `started` receipts to `running`, `completed`, or `failed` when downstream evidence exists |
| `export nc-coverage-assessment` | Generate DB-driven DEP/DEC NC schedule coverage matrices and export Markdown/CSV/JSON under `docs/reports/nc_coverage_assessment/` |
| `export nc-anomaly-audit` | Generate a ranked NC anomaly audit with recommended next actions under `docs/reports/nc_anomaly_audit/` |
| `export nc-schedule-inventory-audit` | Generate a full NC `rate_schedule` family inventory showing what is missing from the focused matrix and what looks legacy/malformed |
| `export nc-document-intelligence-audit` | Apply the new document-intelligence layer to zero-charge and malformed NC historical rows, producing a canonicalization / retire-vs-reclassify queue |
| `export nc-document-gap-audit` | Generate temporal / ordinal / quality-floor gap opportunities for NC DEP/DEC families |
| `export nc-confidence-audit` | Generate a family-level confidence scorecard combining continuity, gap pressure, parse anomalies, quality tier mix, and redline corroboration |
| `export nc-redline-lead-audit` | Generate a ranked redline-hunt queue for families where redline clues can help locate or validate clean companion tariffs |
| `export nc-redline-parse-audit` | Audit parsed NC tariff versions whose linked source PDFs may be redlines; now uses page-bounded slice detection and can distinguish clean exact-date companions |
| `refresh-nc-redline-fingerprints` | Refresh `document_fingerprints.is_redline_candidate/redline_confidence` for NC DEP/DEC PDFs using the corrected redline detector |
| `canonicalize-historical-family-key` | Move a malformed historical family into a canonical family key, updating linked historical lineage tables and repairing orphaned version rows |
| `export dep-leaf-503-audit` | Generate a focused DEP `leaf-503` (`R-TOU-CPP`) version/rider-linkage audit under `docs/reports/dep_leaf_503_audit/` |
| `seed-dep-residential-rider-applicability` | Seed mandatory DEP residential rider-family links for schedules `leaf-500` through `leaf-504` |
| `export dep-residential-rider-gap-audit` | Generate rider-family charge coverage gaps for DEP residential schedules `leaf-500` through `leaf-504` |
| `export dep-residential-rider-action-queue` | Generate a ranked DEP residential rider repair queue derived from the rider-gap audit |
| `export dep-residential-rider-repair-plan` | Generate an operational DEP residential rider repair plan with parser-profile and discovery guidance |
| `export dep-compliance-bundle-audit` | Generate a DEP rider-family bundle audit that distinguishes missing discovery, downloaded-not-imported, unbounded, and under-parsed compliance bundles |
| `export dep-storm-rider-audit` | Generate a DEP storm-rider family audit showing canonical candidates, legacy duplicates, residual parse debt, and missing applicability links |
| `export dep-storm-history-inventory` | Generate a DEP storm-history inventory showing current canonical storm families plus older docket candidates that may contain predecessor storm leaves |
| `seed-dep-storm-rider-applicability` | Seed DEP storm-rider applicability links for residential schedules using the current `Leaf 607` and `Leaf 613` applicability text |
| `show-provenance-gaps-nc` | Summarize missing version provenance fields plus missing/path-only discovery linkage for NC historical rows |
| `show-fingerprint-coverage-nc` | Summarize NC hash-backed coverage, path-only historical rows, document fingerprints, and reusable artifact coverage |
| `show-document-classification-audit-nc` | Classify NC historical documents into routing buckets like `extractable_charge`, `formula_only`, `reference_only`, `redline_candidate`, `unrelated_but_keep`, and `unknown` |
| `show-unknown-routing-audit-nc` | Collapse `unknown` and weak-routing NC rows into family-level recommendations like `new_profile_or_family_routing_review`, `evaluate_formula_or_program_lane`, or `reclassify_non_tariff_or_reference`, and surface synthesized profile candidates / next commands when the family is clearly routable |
| `validate-lineage-nc` | Cross-check NC historical docs for family assignment, provenance debt, and extraction readiness |

Targeted extraction diagnostics:
```powershell
python -m duke_rates extract-rates-nc --family-key nc-progress-leaf-500 --verbose
```

Use `--family-key` to constrain extraction to one family and `--verbose` to print:
- per-status counts
- zero-charge document details with parser profile and effective date
- failed document rows with the exception summary

Parser-selection diagnostics:
```powershell
python -m duke_rates show-parser-selection-audit-nc --limit 25
python -m duke_rates show-parser-improvement-candidates-nc --limit 25
python -m duke_rates show-document-classification-audit-nc --limit 25
python -m duke_rates show-unknown-routing-audit-nc --limit 25
```

Use this before changing parser profiles. It shows:
- how often the latest run ends on `generic_residential`
- which initial->final profile transitions are happening
- which profiles dominate weak/empty latest outcomes
- whether the real issue is parser routing versus formula/reference/redline/unrelated content
- whether the document has no usable text and should enter OCR/Paddle/Docling
  remediation before any parser-profile work
- whether the document already has usable text but has never had a latest
  historical processing run, in which case the next command should queue the
  concrete historical document ids instead of changing parser code
- which unsupported families are most worth new profile work versus reclassification into a different content lane
- which single next command another model should run for the top family

Legacy-state repair:
```powershell
python -m duke_rates repair-legacy-ncuc-data --dry-run
python -m duke_rates repair-legacy-ncuc-data --execute
```

Use this when older rows contain:
- `ncuc_discovery_records.acquisition_method='portal_harvest'`
- malformed `historical_documents.current_document_id` values such as UUID strings

These stale rows can break missing-doc triage and historical row loading unless they are normalized.

**Typical full pipeline sequence:**
```bash
python -m duke_rates ncuc-import-pipeline --all-downloaded
python -m duke_rates bootstrap-missing-versions-nc
python -m duke_rates extract-rates-nc
python -m duke_rates parse-review-summary
```

**Confidence / redline follow-up sequence:**
```bash
python -m duke_rates refresh-nc-redline-fingerprints
python -m duke_rates export nc-confidence-audit
python -m duke_rates export nc-redline-lead-audit
python -m duke_rates export nc-redline-parse-audit
```

Use this stack when the question is not just "what extracted?" but:
- do we have the right documents
- are any parsed versions still tied to redlines
- do clean exact-date companions already exist
- which families most need docket hunting versus reparsing

### 2e. OCR Queue (`ocr` sub-app)

For scanned PDFs that pdfplumber cannot read — routes them through Docling or Tesseract.

> **2026-05-12 refactor:** OCR commands moved from the root namespace to the `ocr` sub-app. Invoke as `python -m duke_rates ocr <command>`. See [CLI_REFACTOR_PLAN.md](/c:/Python/Duke/Standalone/docs/CLI_REFACTOR_PLAN.md) for the broader plan.

| Command | What it does |
|---|---|
| `ocr enqueue-nc` | Add documents to the OCR queue (was `enqueue-ocr-nc`) |
| `ocr show-queue-nc` | Show current OCR queue depth and status. Leads with a status-summary header (`total=N pending=N running=N completed=N failed=N`) (was `show-ocr-queue-nc`) |
| `ocr show-remediation-candidates-nc` | Rank NC historical documents that are likely blocked by missing OCR/plaintext, with a recommended next lane (`queue_ocr_or_paddle` vs `run_docling_or_paddle_structure`) (was `show-ocr-remediation-candidates-nc`) |
| `ocr enqueue-remediation-nc` | Enqueue the `queue_ocr_or_paddle` subset from the remediation audit directly into the OCR queue; defaults to `pytesseract_cpu` and previews with `--dry-run` (was `enqueue-ocr-remediation-nc`) |
| `ocr process-queue-nc` | Process OCR queue (Tesseract-based). Supports `--workers N` for bounded parallel local OCR and `--until-empty` to drain the queue in one invocation. Default `--limit` is 500. Does not apply to portal/search workflows (was `process-ocr-queue-nc`) |
| `ocr process-backlog-nc` | **Canonical OCR backlog workflow.** One-shot: enqueue remediation candidates → drain the Tesseract queue with `--until-empty` → extract rates. Replaces the hand-written `enqueue` + `process` loop + `extract-rates` sequence. Flags: `--workers N`, `--skip-enqueue`, `--skip-extract`, `--company`, `--family-key`, `--enqueue-limit` (was `process-ocr-backlog-nc`) |
| `ocr report-benchmark-nc` | Summarize OCR-backed historical docs, downstream parse outcomes, and recommended remediation lanes when OCR artifacts already exist (was `report-ocr-benchmark-nc`) |
| `mine-docling-nc` | Process documents via Docling (GPU-accelerated layout+text) |
| `run-docling-nc` | Run Docling on NC documents |
| `run-docling-vlm` | Run Docling VLM (vision-language model) mode |
| `process-docling-batch` | Process a batch via Docling |
| `benchmark-docling` | Benchmark Docling throughput |
| `benchmark-document-normalization` | Compare native text extraction, Paddle/PP-Structure normalization, GLM-OCR fallback, and the router on representative PDFs |
| `compare-document-page-text` | Compare native vs Paddle vs GLM text on specific PDF pages, including suspicious-symbol heuristics and expected-token checks for OCR problem cases |
| `benchmark-redline-analysis` | Use GLM image analysis on clean/redline tariff pages to test whether tracked-change evidence, visual redline cues, and before/after relationships are recoverable |
| `gpu-status` | Check GPU and CUDA availability |

### 2f. Reprocessing Queue

| Command | What it does |
|---|---|
| `reprocess enqueue-nc` | Add documents to the reprocess queue based on stale/weak parse signal |
| `reprocess enqueue-stale-nc` | Add specifically stale documents to reprocess queue. Supports `--dry-run` for preview (default is `--execute` for backward compatibility) |
| `reprocess enqueue-parser-improvement-nc` | Enqueue the easy-win parser-improvement cohort (`recommended_action=enqueue_reprocess` from `show-parser-improvement-candidates-nc`). These already have usable text + a working profile but no latest run. Defaults to `--dry-run`; pass `--execute` to enqueue. Add `--process` to drain the queue immediately |
| `reprocess show-queue-nc` | Show pending reprocess queue |
| `reprocess show-stale-nc` | Show running reprocess queue rows that appear stale based on `started_at` age |
| `reprocess show-priority-nc` | Rank queued NC reprocess work by impact category with explanations |
| `reprocess process-queue-nc` | Execute the reprocess queue. Supports `--workers N` for bounded parallel local reparsing and `--until-empty` to drain the queue in one invocation. Default `--limit` is 500. Does not apply to portal/search workflows |
| `reprocess recover-stale-nc` | Return stale running reprocess rows to `pending` so they can be claimed again. Defaults to `--dry-run`; pass `--execute` to mutate queue state |
| `reprocess show-stale-historical-nc` | Show historical docs with stale extraction stage |

---

## 3. Tariff Families

| Command | What it does |
|---|---|
| `build-tariff-families` | Rebuild tariff family index from canonical sources |
| `list-tariff-families` | List all tariff families with optional state/company filters |
| `list-provisional-families` | List provisional (auto-generated) families pending review |
| `show-provisional-review-candidates-nc` | Rank charged NC provisional families by likely-garbage / reclassification signals and print inferred promotion fields |
| `promote-provisional-family` | Promote a provisional family to curated status |
| `retire-historical-document` | Retire/delete a historical document record |
| `deduplicate-tariff-charges` | Deduplicate repeated `tariff_charges` rows inside one or more specific version ids using the natural charge signature |
| `retire-provisional-garbage-nc` | Bulk-retire provisional NC families with no charged content. Use `--execute` to apply; default is `--dry-run`. Preserves families with real charges. |
| `list-historical-only-families` | Families that have no current active tariff (historical only) |
| `list-weak-unbounded-historical-nc` | Historical families with weak or missing version bounds |
| `list-placeholder-heading-historical-nc` | Documents with placeholder section headings |
| `list-redundant-legacy-raw-historical-nc` | Redundant legacy raw entries in historical table |
| `list-bundle-reference-legacy-raw-historical-nc` | Bundle-reference legacy entries |
| `load-dep-provisional-riders` | Load DEP provisional rider definitions |

---

## 4. Historical Data Recovery (Progress NC)

These commands recover historical tariff data from the NCUC portal, OpenEI, predecessor
domain archives, and public notice citations.

| Command | What it does |
|---|---|
| `list-history-progress-nc` | List historical tariff versions for DEP NC |
| `recover-history-progress-nc` | Recover missing historical versions |
| `list-history-chains-progress-nc` | Show version succession chains |
| `show-history-chain-progress-nc` | Show detail for a specific chain |
| `show-history-tariff-progress-nc` | Show tariff details for a version |
| `show-history-coverage-progress-nc` | Coverage summary for a family |
| `list-history-sources-progress-nc` | Sources available for history recovery |
| `inspect-history-gaps-progress-nc` | Detailed gap analysis |
| `recover-history-gaps-progress-nc` | Attempt to fill identified gaps |
| `list-history-notice-links-progress-nc` | Notice citations linking to tariff versions |
| `import-history-progress-nc` | Import recovered history into DB |
| `estimate-history-bill-progress-nc` | Estimate bills using historical rates |
| `list-regulator-gaps-progress-nc` | Regulatory filing gaps |
| `show-regulator-gaps-progress-nc` | Detailed regulatory gap view |
| `generate-regulator-inbox-progress-nc` | Generate inbox entries for regulatory filings |
| `list-bill-relevant-gaps-progress-nc` | Families missing bill-relevant rate data |
| `show-bill-relevant-gaps-progress-nc` | Detail view of bill-relevant gaps |
| `parse-bill-relevant-progress-nc` | Parse bill-relevant historical documents |
| `preview-bill-relevant-history-progress-nc` | Preview what history recovery would yield |
| `recover-bill-relevant-history-progress-nc` | Run history recovery for bill-relevant families |
| `preview-bill-relevant-openei-progress-nc` | Preview OpenEI-sourced history |
| `recover-bill-relevant-openei-progress-nc` | Import OpenEI-sourced history |
| `mine-historical-leads-progress-nc` | Mine citation/docket leads for historical versions |
| `list-historical-leads-progress-nc` | List mined leads |
| `score-historical-leads-progress-nc` | Score leads by quality |
| `ingest-manual-lead-progress-nc` | Manually ingest a lead record |
| `seed-family-documents-progress-nc` | Seed document links for a family |
| `list-unresolved-historical-families-progress-nc` | Families with no linked history docs |
| `preview-root-url-lists-progress-nc` | Preview URL list sources |
| `import-root-url-lists-progress-nc` | Import URL lists as history candidates |
| `generate-search-packs-progress-nc` | Generate search packs for gap families |
| `list-search-packs-progress-nc` | List generated search packs |
| `show-search-pack-progress-nc` | Show detail on a search pack |
| `preview-google-dorks-progress-nc` | Preview Google dork queries |
| `run-google-dorks-progress-nc` | Execute Google dork queries |
| `export-google-dorks-progress-nc` | Export dork results |
| `show-docket-leads-progress-nc` | Show docket-sourced leads |
| `preview-predecessor-domain-progress-nc` | Preview predecessor domain archives |
| `recover-predecessor-domain-progress-nc` | Import from predecessor domain |
| `preview-openei-history-progress-nc` | Preview OpenEI history candidates |
| `recover-openei-history-progress-nc` | Import OpenEI historical records |
| `probe-archive-today-progress-nc` | Probe archive.today for historical versions |
| `preview-history-family-crosswalk-progress-nc` | Preview family crosswalk mapping |
| `apply-history-family-crosswalk-progress-nc` | Apply crosswalk (remap family assignments) |
| `migrate-historical-family-lineage` | Migrate lineage from legacy to new schema |
| `canonicalize-historical-family-key` | Promote malformed `doc-*`/legacy historical families into canonical keys; prefer this over one-off DB edits when the target family already exists |

---

## 5. Search Pipeline

Structured multi-stage search for documents on external portals.

| Command | What it does |
|---|---|
| `search probe-compat` | Check compatibility of a search strategy with available sources |
| `search show-compat` | Show compatibility results |
| `search probe-query` | Probe a candidate query for result quality |
| `search show-results` | Show staged search results |
| `search query-report` | Report on query quality and coverage |
| `search run` | Execute a full search run |
| `search ingest` | Ingest search results into the pipeline |
| `search export` | Export search results |
| `search doc-param` | Parameterized document search |
| `search download-doc-param` | Parameterized download after search |
| `search enrich-doc-param` | Enrich search result with additional metadata |
| `audit-search-worklist` | Audit the active search worklist |

---

## 6. Bill Calculation and Reconstruction

| Command | What it does |
|---|---|
| `calculate-bill` | Calculate a bill for a usage profile + tariff combination |
| `estimate-bill` | Estimate a bill (less precise than calculate-bill) |
| `compare-rates` | Compare rates across two tariff versions |
| `compare-tariff-rates` | Compare charges across tariff families |
| `compare-schedules` | Compare rate schedules side-by-side |
| `compare-version-rates` | Compare two specific version IDs |
| `test-bill-reconstruction-nc` | Test bill reconstruction accuracy against actual bills |
| `reconcile-bill-progress-nc` | Reconcile a bill against DEP rate data |
| `parse-bill` | Parse a single bill document (ESPI/Green Button or PDF) |
| `parse-bills` | Parse a batch of bills |
| `list-bills` | List parsed bills |
| `show-bill` | Show detail on a parsed bill |
| `bill-calculator` | Interactive bill calculator |
| `nc-rate-context` | Show NC-specific rate context for a given date |

---

## 7. Bill Observations

| Command | What it does |
|---|---|
| `derive-bill-observations` | Extract structured observations from parsed bills |
| `list-bill-observations` | List bill observations |
| `list-observed-component-history-progress-nc` | Component history derived from bill observations |
| `show-observed-component-history-progress-nc` | Detail for a specific component |

---

## 8. Parse Review Queue

| Command | What it does |
|---|---|
| `review-queue` | Legacy current-document parse queue. Prefer `parse-review-queue` for historical pipeline work. |
| `parse-review-queue` | Show the full parse review queue |
| `record-parse-review` | Record a manual parse review outcome |
| `reconcile-skipped-parse-reviews` | Reconcile items that were skipped or deferred |
| `parse-review-summary` | **Recommended.** Summary broken down by pipeline stage. The `tiered_ingest / unknown` backlog (~3,300) is legacy and does not block current work. |

---

## 9. EIA Reference Data

| Command | What it does |
|---|---|
| `eia-backfill` | Backfill EIA state electricity price history |
| `eia-update` | Incremental EIA data update |
| `eia-state-price` | Look up EIA state average price |
| `eia-national-comparison` | Compare NC rates against national EIA averages |
| `load-eia-rates` | Load EIA reference rates into DB |

---

## 10. OpenEI / URDB

| Command | What it does |
|---|---|
| `lookup-openei-rates` | Query OpenEI URDB for matching rates |
| `build-openei-export` | Build an export candidate for OpenEI contribution |
| `export-urdb` | Export tariff data in URDB format |

---

## 11. Audits and Analysis

| Command | What it does |
|---|---|
| `audit-local-raw-nc` | Audit locally stored raw NC documents |
| `audit tariff-coverage` | Coverage audit across families and versions |
| `audit tariff-null-scan` | Find null/missing fields in tariff data |
| `audit tariff-timeline` | Timeline integrity audit for version succession |
| `audit-rider-map` | Audit the rider-to-family mapping |
| `cleanup-nc-residential-history` | Remove stale/orphaned residential history artifacts |
| `show-bill-relevant-gaps-progress-nc` | (see §4) |

---

## 12. Local Data Loading

| Command | What it does |
|---|---|
| `load-local-rates-nc` | Load locally stored rate files into DB |
| `load-local-rider-summaries-nc` | Load locally stored rider summary files |
| `load-dep-provisional-riders` | Load DEP provisional rider table |
| `load-ncuc-ingest` | Load raw NCUC ingest records from file |

---

## 13. MCP Server

| Command | What it does |
|---|---|
| `mcp` | Start the MCP (Model Context Protocol) server. Enables Claude and other LLM agents to call duke-rates tools programmatically via the MCP protocol. |

**Usage:** `python -m duke_rates mcp`  
**Source:** `src/duke_rates/mcp/server.py`

---

## 14. Hardware

| Command | What it does |
|---|---|
| `gpu-status` | Check GPU availability, CUDA version, and device count. Useful before running Docling. |

---

## 15. Document Intelligence (Classification, Embeddings, LLM)

Commands for multi-stage document classification, embedding generation, and LLM adjudication.
Built in Phases 2–5.5 of [document_intelligence_roadmap.md](/c:/Python/Duke/Standalone/docs/document_intelligence_roadmap.md).

### 15a. Taxonomy and Classification (Phase 2)

| Command | What it does |
|---|---|
| `list-document-types-nc` | List the document_type taxonomy (code, category, description, is_terminal) |
| `report-document-types-nc` | Show document_type classification distribution across the corpus |
| `report-classification-disagreements-nc` | Surface low-margin, override, and cross-classifier disagreements. `--cross-stage document_type` compares rule vs embedding. `--stage flag_is_final` etc. for flag-stage low-confidence rows |
| `report-flag-classifications-nc` | Show flag classification distribution across all 11 flag stages |

### 15b. Flag Classifiers (Phase 3)

| Command | What it does |
|---|---|
| `backfill-flag-classifications-nc` | Run all 11 flag classifiers against existing historical_documents. `--limit`, `--dry-run`, `--progress/--no-progress` |

Flag stages: `flag_is_final`, `flag_is_proposed`, `flag_is_redline`, `flag_is_confidential`, `flag_has_rate_tables`, `flag_has_leaf_numbers`, `flag_is_compliance_filing`, `utility`, `docket_number`, `effective_date`, `tariff_family`.

### 15c. Ollama Model Orchestration (Phase 2.5)

| Command | What it does |
|---|---|
| `check-ollama-models-nc` | Health-probe all configured Ollama roles and report availability |
| `benchmark-ollama-roles-nc` | Compare explicit local Ollama models against production-style document-intelligence prompts/schemas without mutating DB rows. Initial tasks: `parse_diagnosis`, `hard_parse_diagnosis`, `regex_suggestion`, `structured_rate_extraction`, `document_classification`. Writes JSON reports to `docs/reports/ollama_model_benchmarks/`. |
| `run-llm-doc-probe-nc` | Smoke-test a role against a document or ad-hoc text; validates prompt+JSON schema. `--persist` writes to `document_classifications` |

Benchmark examples:
```bash
python -m duke_rates benchmark-ollama-roles-nc --task parse_diagnosis --models gemma4:e4b-it-q4_K_M,qwen3:8b,mistral:7b-instruct,phi3.5:latest --limit 10 --max-runtime-minutes 90
python -m duke_rates benchmark-ollama-roles-nc --task all --models gemma4:e4b-it-q4_K_M,qwen3:8b,mistral:7b-instruct,phi3.5:latest --limit 5 --timeout-s 120 --max-runtime-minutes 360
python -m duke_rates benchmark-ollama-roles-nc --task parse_diagnosis,regex_suggestion,structured_rate_extraction --models gemma4:e4b-it-q4_K_M,qwen3:8b,phi3.5:latest --limit 5 --max-runtime-minutes 240
python -m duke_rates benchmark-ollama-roles-nc --task regex_suggestion --limit 5 --max-runtime-minutes 90
python -m duke_rates benchmark-ollama-roles-nc --task structured_rate_extraction --limit 5 --timeout-s 90 --max-runtime-minutes 120
python -m duke_rates benchmark-ollama-roles-nc --task document_classification --limit 20 --max-runtime-minutes 90
```

Gold-fixture scoring:
```bash
python -m duke_rates benchmark-ollama-roles-nc --task parse_diagnosis --models gemma4:e4b-it-q4_K_M,qwen3:8b --limit 10 --fixtures docs/reports/ollama_model_benchmarks/gold_fixtures.json
python -m duke_rates benchmark-ollama-roles-nc --task all --models gemma4:e4b-it-q4_K_M,qwen3:8b,mistral:7b-instruct,phi3.5:latest --limit 10 --timeout-s 120 --max-runtime-minutes 360 --fixtures docs/reports/ollama_model_benchmarks/gold_fixtures.json
```

Fixture shape:
```json
{
  "fixtures": [
    {
      "task": "parse_diagnosis",
      "case_id": "parse_attempt:46880",
      "expected": {
        "failure_type": "regex_gap",
        "recommended_action": "suggest_regex",
        "actionable": true
      }
    }
  ]
}
```

### 15d. Embedding Generation (Phase 4)

| Command | What it does |
|---|---|
| `embed-corpus-nc` | Generate embeddings for all historical_documents (embedding_primary + embedding_secondary roles). Populates `document_embeddings`. Idempotent. `--limit`, `--refresh`, `--kind` (full_text/first_3_pages/title_block/rate_table_text/order_conclusion_section), `--max-chars` (default 2000) |
| `backfill-embedding-classifications-nc` | Run embedding KNN classifier on docs with reference embeddings; persists `embedding_knn_v1` row. `--limit`, `--dry-run` |

Typical embedding workflow:
```bash
python -m duke_rates embed-corpus-nc --limit 100
python -m duke_rates backfill-embedding-classifications-nc --limit 20
python -m duke_rates report-classification-disagreements-nc --cross-stage document_type
```

### 15e. LLM Adjudication (Phase 5)

| Command | What it does |
|---|---|
| `adjudicate-classifications-nc` | Run LLM (balanced_classifier role) on document_type disagreements. Persists `llm_<model>_v1` row. Does NOT auto-supersede. `--limit` (default 10), `--dry-run`, `--json` |

### 15f. Overnight Document Intelligence Loop (Phase 5.5)

| Command | What it does |
|---|---|
| `run-overnight-doc-intelligence-nc` | Resumable unattended batch: embed + LLM adjudication in sequence per document. Safety: no destructive writes, bounded by wall-clock cap, `--resume` skips completed tuples, stops on health probe degradation / consecutive failures. End-of-run JSON at `docs/reports/overnight_doc_intelligence/<ts>.json`. |

Flags:
```
--max-documents N            (0 = unlimited)
--max-runtime-minutes N      (0 = unlimited, wall-clock cap)
--max-consecutive-failures N (default 5)
--stages embed,llm_adjudicate (comma-separated; default both)
--since ISO8601              (only docs added after T)
--dry-run                    (enumerate work set, no model calls)
--resume                     (skip completed subject+stage+model+prompt_version)
--progress-interval N        (default 10)
--health-probe-interval N    (default 50)
```

Overnight workflow:
```bash
python -m duke_rates run-overnight-doc-intelligence-nc --dry-run
python -m duke_rates run-overnight-doc-intelligence-nc --max-runtime-minutes 120 --resume
python -m duke_rates run-overnight-doc-intelligence-nc --stages llm_adjudicate --max-documents 20
```

### 15g. LLM-Assisted Parse Diagnosis & Improvement (Phase 5.6)

| Command | What it does |
|---|---|
| `analyze-parse-failures-nc` | Query `parse_attempt_logs` for weak/empty parses, run LLM root-cause diagnosis (`parse_failure_triage` role; currently `mistral:7b-instruct` after fixture-backed benchmark). Escalates low-confidence results to `hard_parse_diagnosis`. Persists to `llm_parse_diagnostics`. |
| `suggest-regex-fixes-nc` | Generate regex/normalization suggestions for diagnosed failures. Uses `regex_suggestion` role. Exports review artifacts to `docs/reports/regex_suggestions/`. Suggestions NEVER auto-applied. |
| `validate-regex-suggestions-nc` | Deterministic validation harness — tests candidate regexes against known-good, known-failed, and unrelated documents. No LLM calls (local regex testing). Does NOT modify parser code. |
| `run-llm-parse-fallback-nc` | Schema-guided LLM fallback extraction for weak/empty parses. Uses `structured_rate_extraction` role. Rows stored as CANDIDATES only — never production. |
| `validate-llm-rate-extractions-nc` | Deterministically validate LLM candidate rate rows against source text. Checks source-quote grounding, value grounding, and unit evidence. Defaults to report-only; `--execute` updates candidate extraction status to `validated`, `review_candidate`, or `rejected` and writes row-level advisory records to `llm_candidate_rate_row_validations`, never production charges. |
| `locate-llm-row-evidence-nc` | Focused LLM pass over unresolved row validations. Asks the model to locate exact unit evidence, then deterministically verifies the evidence quote before optionally writing advisory repairs to `llm_candidate_rate_row_repairs`. |
| `reclassify-llm-row-conflicts-nc` | Focused LLM pass over row validations with `unit_conflicts_with_inferred`. Proposes advisory charge-type/unit repairs and stores accepted/rejected repair evidence separately from production charges. |
| `apply-deterministic-llm-row-repairs-nc` | Create advisory repairs for repeated deterministic row-validation patterns, such as lighting table rows where accepted evidence already proves `Lighting Charge` and `$/month`. |
| `show-llm-row-effective-status-nc` | Report row-level validation status after accounting for accepted advisory repairs (`validated_with_repair`). |
| `propose-llm-charge-promotions-nc` | Build proposal rows for validated/effective LLM rate rows after lineage, version, duplicate, and conflict checks. Does not insert production charges. |
| `promote-llm-charge-proposals-nc` | Promote eligible, novel, conflict-free LLM charge proposals into `tariff_charges`; dry-run by default and audited on execute. |
| `run-llm-promotion-overnight-nc` | Run the guarded deterministic promotion-maintenance loop: validate candidate/review extractions, apply deterministic repairs, create proposals for newly validated rows, refresh existing proposals, dry-run promotion, optionally safe-execute promotions, and write a timestamped JSON morning report under `docs/reports/llm_promotion_overnight/`. |
| `run-overnight-parse-improvement-nc` | Resumable unattended batch. For backlog reduction, prefer `diagnose,extract_staged`; regex suggestion / grounded-rule tasks remain useful for parser research but are not the default production-oriented path. Same safety pattern as Phase 5.5 (wall-clock cap, resume, dry-run, SIGINT/SIGTERM). End-of-run JSON at `docs/reports/overnight_parse_improvement/<ts>.json`. |

Diagnosis flags:
```
--limit N                    (default 25)
--profile TEXT               (optional parser profile filter)
--family TEXT                (optional family_key filter)
--since ISO8601              (only parse attempts after T)
--rediagnose-unknown         (append fresh diagnoses for prior unknown/0.0 rows)
--dry-run                    (enumerate candidates, no LLM calls)
--json                       (emit JSON report)
```

Suggestion flags:
```
--limit N                    (default 10)
--diagnosis-id ID            (target specific diagnosis)
--profile TEXT               (optional parser profile filter)
--failure-type TEXT          (regex_gap | normalization_gap | ocr_noise)
--dry-run / --json
```

Extraction flags:
```
--limit N                    (default 10)
--historical-document-id ID  (target specific doc)
--profile TEXT / --family TEXT
--dry-run / --json
```

Overnight loop flags:
```
--max-documents N            (0 = unlimited)
--max-runtime-minutes N      (0 = unlimited, wall-clock cap)
--max-consecutive-failures N (default 5)
--task-kind diagnose,extract_staged
--profile TEXT / --family TEXT
--since ISO8601
--rediagnose-unknown         (diagnose stage re-runs prior unknown/0.0 rows)
--dry-run / --resume
--limit N                    (default 25)
```

Overnight workflow:
```bash
python -m duke_rates show-llm-row-effective-status-nc --json
python -m duke_rates propose-llm-charge-promotions-nc --limit 10000 --refresh-existing --json
python -m duke_rates promote-llm-charge-proposals-nc --limit 500 --json

# Short iterative loop while tuning prompt/model behavior or measuring blocker reduction.
python -m duke_rates run-overnight-parse-improvement-nc --task-kind diagnose,extract_staged --max-runtime-minutes 10 --limit 10 --resume --auto-rediagnose-unknown
python -m duke_rates run-llm-promotion-overnight-nc --validation-limit 500 --repair-limit 1000 --proposal-limit 10000 --promotion-limit 500 --json

# Full overnight extraction-first run after the short loop is productive.
python -m duke_rates run-overnight-parse-improvement-nc --task-kind diagnose,extract_staged --max-runtime-minutes 360 --limit 100 --resume --auto-rediagnose-unknown
python -m duke_rates run-llm-promotion-overnight-nc --validation-limit 2000 --repair-limit 4000 --proposal-limit 20000 --promotion-limit 1000 --json

# Optional targeted repair passes.
python -m duke_rates validate-llm-rate-extractions-nc --limit 2000 --execute
python -m duke_rates locate-llm-row-evidence-nc --issue unit_missing --limit 250 --execute
python -m duke_rates reclassify-llm-row-conflicts-nc --limit 250 --execute
python -m duke_rates apply-deterministic-llm-row-repairs-nc --limit 2000 --execute
python -m duke_rates propose-llm-charge-promotions-nc --limit 10000 --execute
python -m duke_rates propose-llm-charge-promotions-nc --limit 10000 --refresh-existing --execute
python -m duke_rates promote-llm-charge-proposals-nc --limit 500 --json

# Use only after dry-run output is clean.
python -m duke_rates run-llm-promotion-overnight-nc --validation-limit 2000 --repair-limit 4000 --proposal-limit 20000 --promotion-limit 250 --execute-safe --json
```

Operational notes:
- `extract_staged` is now the recommended overnight value-creation path. It has been more productive than asking local models to synthesize reusable regex.
- If `extract_staged` repeatedly reports `filtered_at_stage_1` with the same
  `regex_gap` / `wrong_profile` distribution, the LLM lane is idle. Move to
  parser-profile/routing fixes before running another long extraction loop.
- `run-llm-promotion-overnight-nc` now has two proposal phases:
  - create proposals for newly validated rows
  - refresh existing pending proposals
- Promotion remains dry-run unless `--execute` or `--execute-safe` is supplied.
- Recommended blocker order:
  1. `missing_version_effective_start`
  2. `malformed_family_key`
  3. `unqualified_rate_unit`
  4. `unsupported_charge_type`
  5. `ambiguous_numeric_table_row`
- `ambiguous_numeric_table_row` is an intentional safety hold for broad multi-number summary/table lines where the selected value may be column-ambiguous.
- Proposal rerouting can already exploit:
  - same-family dated sibling versions inferred from historical snapshot/effective metadata
  - bounded bundle rerouting via unique leaf/date evidence
  - Leaf 601 BA-like summary-line dates when a unique rider-summary match points to an existing dated Leaf 601 version.

### 15h. One-Hour Targeted LLM Loop

Use this when you want to test whether the LLM lane can improve the backlog
without a full overnight run. Keep the scope narrow: `generic_residential` and
`progress_single_value_rider` are the current high-value profiles.

For a reusable launcher, use
[`scripts/overnight/targeted_llm_blocker_loop.ps1`](/c:/Python/Duke/Standalone/scripts/overnight/targeted_llm_blocker_loop.ps1).

```powershell
# Baseline and routing diagnostics
python -m duke_rates show-workflow-status-nc
python -m duke_rates show-parser-improvement-candidates-nc --limit 25
python -m duke_rates show-near-miss-profiles-nc --limit 25
python -m duke_rates show-unknown-routing-audit-nc --limit 25

# Routing-impact enqueue and queue drain
python -m duke_rates reprocess show-stale-nc --limit 10
python -m duke_rates reprocess recover-stale-nc --limit 10 --older-than-minutes 240 --execute
python -m duke_rates reprocess enqueue-profile-impact-nc --parser-profile progress_single_value_rider --limit 25 --requested-by targeted_llm_blocker_loop
python -m duke_rates reprocess enqueue-profile-impact-nc --parser-profile generic_residential --limit 25 --requested-by targeted_llm_blocker_loop
python -m duke_rates reprocess enqueue-profile-impact-nc --parser-profile zero_charge_program --limit 25 --requested-by targeted_llm_blocker_loop
python -m duke_rates reprocess enqueue-profile-impact-nc --parser-profile progress_current_leaf_bridge --limit 25 --requested-by targeted_llm_blocker_loop
python -m duke_rates reprocess process-queue-nc --limit 25 --workers 4

# Extraction passes
python -m duke_rates run-overnight-parse-improvement-nc --task-kind extract_staged --max-runtime-minutes 15 --limit 10 --resume --auto-rediagnose-unknown --profile progress_single_value_rider
python -m duke_rates run-overnight-parse-improvement-nc --task-kind extract_staged --max-runtime-minutes 15 --limit 10 --resume --auto-rediagnose-unknown --profile generic_residential

# Deterministic cleanup
python -m duke_rates validate-llm-rate-extractions-nc --limit 200 --execute
python -m duke_rates locate-llm-row-evidence-nc --issue unit_missing --limit 50 --execute
python -m duke_rates reclassify-llm-row-conflicts-nc --limit 50 --execute
python -m duke_rates apply-deterministic-llm-row-repairs-nc --limit 200 --execute

# Promotion refresh
python -m duke_rates propose-llm-charge-promotions-nc --limit 10000 --refresh-existing --execute --json
python -m duke_rates promote-llm-charge-proposals-nc --limit 500 --execute --json
python -m duke_rates show-llm-row-effective-status-nc --json
python -m duke_rates show-workflow-status-nc
```

Stop early if:
- the routing diagnostics keep surfacing the same top families with no enqueue impact
- the extraction passes are idle or filtered at stage 1
- promotion still evaluates to `0 promotable` after cleanup

This is a routing-first backlog-reduction test, not a full backlog sweep.

### 15h1. Routing-First Overnight Loop Until 9am

Use this when you want an overnight loop that spends most of its time on the
highest-leverage backlog reduction path: routing unknown families into explicit
profiles and draining the impacted reprocess queue.

For the reusable launcher, use
[`scripts/overnight/routing_first_until_9am.ps1`](/c:/Python/Duke/Standalone/scripts/overnight/routing_first_until_9am.ps1).

```powershell
pwsh scripts\overnight\routing_first_until_9am.ps1 -DeadlineTime "09:00"
```

This loop:
- reads `show-unknown-routing-audit-nc --json` each cycle
- enqueues profile-impact work for synthesized existing-profile candidates
- drains `reprocess process-queue-nc --until-empty`
- re-checks workflow status before the next cycle

Use this instead of the OCR-heavy backlog drain when the main bottleneck is
still `unknown_profile` routing and reprocess backlog, not OCR throughput.

### 15i. Multi-Phase Backlog-Drain Wrapper

Use the wrapper when multiple lanes have real work: OCR remediation, stale
reprocess, bootstrap, and LLM promotion. Do not use it as a blind replacement
for targeted blocker work.

```powershell
pwsh scripts\overnight\backlog_drain_overnight.ps1 `
  -DeadlineTime "08:00" `
  -MaxSliceMinutes 30 `
  -OcrEnqueueLimit 25 `
  -OcrWorkers 4 `
  -ReprocessLimit 20 `
  -ReprocessWorkers 2 `
  -BootstrapLimit 50 `
  -ExtractLimit 12 `
  -GroundedLimit 10
```

Preflight:

```powershell
python -m duke_rates show-workflow-status-nc
python -m duke_rates ocr show-queue-nc --status all --limit 10
python -m duke_rates ocr show-remediation-candidates-nc --limit 25
python -m duke_rates reprocess show-queue-nc --status running --limit 10
python -m duke_rates propose-llm-charge-promotions-nc --limit 10000 --refresh-existing --json
```

Morning readout:

```powershell
python -m duke_rates show-workflow-status-nc
python -m duke_rates ocr report-benchmark-nc --limit 50
python -m duke_rates ocr show-remediation-candidates-nc --limit 25
python -m duke_rates reprocess show-queue-nc --status running --limit 10
python -m duke_rates promote-llm-charge-proposals-nc --limit 500 --json
```

Wrapper behavior notes:
- The wrapper uses environment-backed scalar SQL probes instead of inline
  `SELECT COUNT(*)` strings, avoiding PowerShell glob parsing errors around `*`.
- Bootstrap now skips the expensive full `extract-rates-nc` call when
  `bootstrap-missing-versions-nc` creates no new linked versions.
- `null_effective_start` is reported as a separate blocker. Use
  `workflow remediate-nc-missing-doc-effective-start`; bootstrap cannot fix historical
  docs that already have versions but lack effective dates.
- If OCR remediation keeps cycling the same families, inspect
  `ocr show-remediation-candidates-nc` and `ocr report-benchmark-nc`. Completed
  OCR should no longer be treated as absent solely because the artifact hash and
  historical document hash differ.

Model note:
- `parse_failure_triage` should favor fixture accuracy over action rate. A May
  2026 fixture-backed benchmark found `mistral:7b-instruct` was the only tested
  model with nonzero parse-diagnosis gold accuracy. It is biased toward
  `wrong_profile`, so keep expanding the fixture set before treating it as final.
- `structured_rate_extraction` uses `gemma4:e4b-it-q4_K_M`; it was the only
  viable tested extraction model in the first fixture-backed benchmark, with
  100% gold accuracy on valid returns and no timeouts.
- `regex_suggestion` uses `qwen3:8b`. After expanding regex gold fixtures from
  1 to 2 cases, `qwen3:8b`, `mistral:7b-instruct`, and `phi3.5:latest` all
  reached 100% valid JSON and 100% gold accuracy; `qwen3:8b` was the fastest
  among those fully valid models. `gemma4:e4b-it-q4_K_M`, `nemotron-3-nano:4b`,
  and `ministral-3:8b` each failed schema validation on one of two cases. The
  first fixture-backed run exposed schema-normalization gaps rather than true
  model failures: local models emitted `risk_level` and string test cases, both
  now accepted by `RegexSuggestion`. The prompt now explicitly asks for
  confidence.
- Regex fixtures now include `diagnosis:55` and `diagnosis:69`. Fixture-backed
  regex benchmarks include explicit diagnosis IDs even when an older suggestion
  row already exists, and the regex excerpt helper favors rate/unit/incentive
  lines in long documents.
- `smallthinker:latest` should not be used for this role; it is fast but mostly
  punts to `unknown` or low-confidence labels. `llama3.1:8b-instruct-q4_K_M`
  failed schema validation for this task, and `qwen2.5-coder:7b` showed a 20%
  schema-error rate in the 10-case benchmark.
- `hard_parse_diagnosis` still uses `qwen2.5-coder:7b`; benchmark it separately
  before changing that role because the current 8-model run measured the normal
  parse-diagnosis prompt, not hard escalation quality.
- Orchestrator fallbacks only run on timeout/HTTP/schema failure. They do not
  provide a quality second opinion when the primary returns valid but biased
  JSON; a future ensemble/validator mode would be needed for that.
- Existing `llm_parse_diagnostics` rows are treated as already diagnosed. A
  model-role change affects new candidates, not old `unknown` diagnoses, unless
  `--rediagnose-unknown` is used. Re-diagnosis is append-only; it does not
  delete or overwrite the prior diagnostic row.
- For long unattended runs, prefer staged execution: diagnose or
  `--rediagnose-unknown` first, then `suggest`, then `validate`. Run `extract`
  separately after diagnosis/suggestion quality is understood, because schema
  extraction is the heaviest Ollama stage.
- Use `benchmark-ollama-roles-nc` before changing role mappings when adding a
  new local model. The benchmark reports JSON validity, latency, tokens/sec,
  confidence, and task-specific actionable-output rates without inserting
  diagnostic, suggestion, classification, or extraction rows.
- Use `--task all` or a comma-separated task list before introducing layered
  specialist roles. Multi-task reports add per-task rankings, label-bias
  scores, diversity counts, and model-pair disagreement rates so a model can
  be selected for the task it is actually good at instead of being ranked as a
  generic "best LLM."
- Add `--fixtures path/to/gold_fixtures.json` when a human-reviewed expected
  answer set exists. This adds `accuracy_pct` to model summaries. Without
  fixtures, the benchmark is useful for schema validity, speed, bias, and
  complementarity, but not true accuracy.

### 15h. Architecture Principles

- **Rule first, embeddings second, LLM third.** No classifier auto-supersedes another.
- **One stage, one row per classifier.** Each decision is separate `document_classifications` row on `(subject_kind, subject_id, stage, classifier, classifier_version)`.
- **UNKNOWN is a valid label.** Documents that don't match land as UNKNOWN — this surfaces new types.
- **LLM evidence is not load-bearing.** LLM labels only "count" once confirmed via Phase 6 human review.
- **All classify commands are idempotent.** Re-running with same classifier_version updates confidence/evidence without duplicating rows.

---

## Key Flags and Options

Most commands support these patterns:

```bash
--state NC             # filter to NC
--company progress     # filter to DEP (Duke Energy Progress)
--company carolinas    # filter to DEC (Duke Energy Carolinas)
--dry-run              # show what would happen without writing
--limit N              # cap output or processing count
--all-downloaded       # process all pending downloads (ncuc-import-pipeline)
--auto-parse           # parse immediately after download (tariff-update)
--parser-profile NAME  # target a specific profile (reprocess enqueue-profile-impact-nc)
```

---

## Known Tooling Gaps

These are recurring manual steps that are not yet covered by a CLI command.
They represent the highest-leverage additions for future agents to build.

### P2 — Fingerprint Coverage

Implemented: `show-fingerprint-coverage-nc`

### P2 — Ranked Reprocess Priority

Implemented: `reprocess show-priority-nc`

### P3 — Provisional Family Auto-Scoring

**Implemented:** `show-provisional-review-candidates-nc`  

Scores charged provisional NC families by key length, known-garbage title patterns, charge quality, and span-fragment signals. The command is read-only and also prints inferred promotion fields so agents can promote reviewed families without inventing titles or schedule codes.

### P3 — Extraction Coverage Dashboard

**Implemented:** `show-extraction-coverage-nc`

Shows per-family version counts, charges, and coverage percentage ranked by gap size. Also reports full-coverage vs partial vs no-coverage families.

### P1 — Document Diagnostic

**Implemented:** `diagnose-document-nc --hd-id X`

Shows the full pipeline path for one historical document: profile selection reasoning, ranked candidate profiles with scores, signal detection, text metrics, fallback chain, and a recommended next action. Supports `--show-text` to inspect raw parser input and `--json` for machine-readable output.

### P3 — Family-Level Deduplication

**Implemented:** `deduplicate-family-nc --family-key X`

Deduplicates tariff charges across all versions in a family using the natural charge signature. Supports `--dry-run` / `--execute`.

### P2 — Bulk doc-* Family Canonicalization

**Implemented:** `canonicalize-doc-families-nc`

Scans remaining doc-* families, infers canonical schedule/rider keys from document titles, and supports bulk `--execute` to promote all eligible families at once. Shows the proposed mapping in dry-run mode.

### P2 — Parser Change Validation

**Implemented:** `validate-parser-change-nc --profile X`

Re-extracts documents affected by a parser profile change and reports before/after charge-count differences to catch regressions before they are committed.

---

## 16. Database Intelligence / Corpus Maintenance (Phase 6.5+)

Commands for detecting and correcting corpus-level quality issues: duplicates,
missing evidence, docket coverage gaps, content-hash integrity, and stale artifacts.
Built as part of the autonomous loop: **detect → decide → act → measure**.

See [document_intelligence_roadmap.md](/c:/Python/Duke/Standalone/docs/document_intelligence_roadmap.md) for architecture context.

### 16a. Overnight Intelligence Report

| Command | What it does |
|---|---|
| `run-overnight-db-intelligence-nc` | Run all 7 deterministic sub-reports + LLM summarization + anomaly identification. Outputs `docs/reports/database_intelligence/<date>.json` and `_summary.json` + `_morning.json`. Safe for unattended execution: `--max-runtime`, `--resume`, `--dry-run`. |
| `report-database-intelligence-nc` | Run the 7 deterministic sub-reports only (no LLM). `--section` filter, `--json` output. |
| `summarize-database-intelligence-nc` | Run LLM summarization against an existing report JSON. Requires Ollama `balanced_classifier` role. |

Sub-reports (all 7 run automatically by the overnight command):
- `missing_versions` — families with version-timeline gaps
- `unknown_documents` — UNKNOWN-classified documents in clusters
- `low_quality_parses` — weak/empty parse attempts (zero charge counts)
- `stale_artifacts` — documents with missing/stale page or span artifacts
- `duplicate_documents` — content-hash duplicates (up to 42 copies of one hash)
- `family_lineage_gaps` — broken or incomplete version chains
- `docket_coverage` — sparse NCUC docket coverage (5 dockets → gap analysis)

Overnight workflow:
```bash
python -m duke_rates run-overnight-db-intelligence-nc --limit 50 --max-runtime 120
python -m duke_rates summarize-database-intelligence-nc --report-path docs/reports/database_intelligence/2026-05-01.json
```

### 16b. Corrective Tools

| Command | What it does | Risk |
|---|---|---|
| `deduplicate-documents-nc` | Consolidate historical documents that share the same `content_hash`. Keeps best survivor (most charges, has local_path, newest retrieved_at) and remaps all FK references. `--dry-run`/`--execute`, `--file-hash`, `--limit`, `--json`. | Low |
| `backfill-evidence-nc` | Regenerate `evidence_json` for documents where it's null/empty. Extracts best family-match score breakdown from existing `ncuc_span_artifacts.evidence_score_breakdown_json`. `--dry-run`/`--execute`, `--limit`, `--family`, `--json`. | Low |
| `backfill-content-hash-nc` | Calculate SHA-1 checksums for `historical_documents` where `content_hash` is null or empty (prerequisite for span-artifact matching and evidence backfill). Skips docs whose files are missing on disk. `--dry-run`/`--execute`, `--limit`, `--json`. | Low |
| `recommend-missing-dockets-nc` | Rank dockets with low or zero processed coverage for targeted fetching. Cross-references `ncuc_discovery_records` against `tariff_versions` and `historical_documents` by docket number. Also surfaces `regulatory_docket_leads` with no discovery records. `--utility`, `--min-year`, `--docket`, `--json`. | Read-only |

Typical corrective sequence:
```bash
# 1. Ensure all docs have content_hash (prerequisite)
python -m duke_rates backfill-content-hash-nc --dry-run
python -m duke_rates backfill-content-hash-nc --execute

# 2. Enqueue and process stale docs (regenerates span artifacts with classification)
python -m duke_rates reprocess enqueue-stale-nc --limit 200
python -m duke_rates reprocess process-queue-nc --limit 200

# 3. Backfill evidence from fresh span artifacts
python -m duke_rates backfill-evidence-nc --dry-run
python -m duke_rates backfill-evidence-nc --execute

# 4. Deduplicate identical documents
python -m duke_rates deduplicate-documents-nc --dry-run
python -m duke_rates deduplicate-documents-nc --execute --limit 50

# 5. Identify docket gaps
python -m duke_rates recommend-missing-dockets-nc --json
```

### 16c. Autonomous Loop Controller

| Command | What it does |
|---|---|
| `run-autonomous-cycle-nc` | Run one full autonomous loop cycle: detect (7 reports) → decide (action registry ranking) → act (execute corrective commands) → measure (re-run reports, compute delta). Safe by default (`--dry-run`). `--max-runtime`, `--max-actions`, `--limit`, `--json`. |
| `run-continuous-loop-nc` | Run the continuous autonomous loop with acquisition. When corrective actions on existing data are exhausted, fetches new dockets from NCUC portal → import → bootstrap → extract. Designed for unattended 8-24 hour runs. Stops on max runtime, max cycles, or 2 consecutive no-improvement cycles. Requires NCID + Playwright for portal acquisition; gracefully degrades without auth. `--max-runtime` (default 480m = 8h), `--max-cycles` (default 20), `--max-dockets` (default 2/cycle), `--sleep` (default 300s between cycles). |

Architecture:
```
run-autonomous-cycle-nc
  ├─ 1. DETECT  → build_database_intelligence_report() [7 sub-reports]
  ├─ 2. DECIDE  → decide_actions() [action_registry.py — maps findings → commands with risk/safety caps]
  ├─ 3. ACT     → subprocess.run(corrective CLI command) [--execute required]
  └─ 4. MEASURE → re-run reports, compute before/after delta per category
```

The action registry (`src/duke_rates/document_intelligence/action_registry.py`) maps all 7 finding categories to corrective commands with:
- Severity thresholds (critical=1, high=5, medium=10, low=25)
- Maximum actions per cycle (safety cap)
- Risk assessment and estimated impact
- Measurement strategy for each action

Autonomous loop workflow:
```bash
# Preview decisions (safe, no writes)
python -m duke_rates run-autonomous-cycle-nc --dry-run --json

# Execute 2 corrective actions with bounded safety caps
python -m duke_rates run-autonomous-cycle-nc --execute --max-actions 2 --max-runtime 30

# Full overnight with continuous acquisition (8 hours, unattended)
python -m duke_rates run-continuous-loop-nc --execute --max-runtime 480 --max-cycles 20 --max-dockets 2

# Dry-run the continuous loop first (fast — no portal calls)
python -m duke_rates run-continuous-loop-nc --dry-run --max-cycles 3 --sleep 1
```

---

## Scripts Quick Reference

For scripts that are not yet promoted to CLI commands, see [scripts/README.md](../scripts/README.md).

**Most useful scripts for agents:**

| Script | Purpose |
|---|---|
| `scripts/maintenance/audit_stranded_ncuc_family_clues.py` | Wrapper around `suggest-family-links-nc`; kept for compatibility with older workflows |
| `scripts/maintenance/audit_historical_family_mismatches.py` | Find historical doc / family assignment inconsistencies |
| `scripts/debug/check_new_charges.py` | Quick count of charges by family after extraction |
| `scripts/debug/final_charge_summary.py` | Full charge summary by family |
| `scripts/debug/inspect_db.py` | Run ad hoc SQLite queries against the DB |
| `scripts/ingestion/download_ncuc_portal_documents.py` | Download documents from NCUC portal (requires Chrome + NCID auth in `.env`) |

---

## Optional Dependency Groups

Some commands require optional extras installed via `pip install -e ".[group]"`:

| Group | Commands that need it |
|---|---|
| `browser` | `ncuc-playwright-discover`, `ncuc-portal-scrape`, portal download scripts |
| `pdf` | All PDF parsing and extraction commands |
| `ocr` | `ocr process-queue-nc`, `ocr enqueue-nc` |
| `docling` | `mine-docling-nc`, `run-docling-nc`, `run-docling-vlm`, `process-docling-batch` |
| `ai` | `run-docling-vlm`, LLM-based classification commands |
| `viz` | `app/streamlit_*.py` apps |
| `mcp` | `mcp` server command |

Install all: `pip install -e ".[browser,pdf,ocr,docling,ai,viz,mcp]"`
