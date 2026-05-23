# Next Session: Start Here
**Date Updated:** 2026-05-13 (overnight backlog-drain follow-up + OCR loop guard)
**Purpose:** Short operational handoff with current state, immediate priorities, and the correct entry docs

> **CLI refactor in flight on branch `refactor/cli-sub-apps`.** The 7 OCR commands moved into an `ocr` sub-app: `python -m duke_rates ocr <command>` (e.g. `ocr show-queue-nc`, `ocr process-backlog-nc`). See [CLI_REFACTOR_PLAN.md](/c:/Python/Duke/Standalone/docs/CLI_REFACTOR_PLAN.md) for the full plan — 9 more sub-apps (`doc-intel`, `ncuc`, `lineage`, etc.) are pending. All in-tree references (suggestion strings, tests, agent docs) were updated in the pilot commit; further phases will follow the same pattern.

## Read First

Read these before broad repo exploration:

1. [AGENT_ONBOARDING.md](/c:/Python/Duke/Standalone/AGENT_ONBOARDING.md)
2. [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md)
3. [agent_tool_use_policy.md](/c:/Python/Duke/Standalone/docs/agent_tool_use_policy.md)
4. [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)
5. [NEXT_SESSION_PRIORITIES.md](/c:/Python/Duke/Standalone/docs/NEXT_SESSION_PRIORITIES.md)
6. [document_intelligence_roadmap.md](/c:/Python/Duke/Standalone/docs/document_intelligence_roadmap.md) — read before extending classification, fingerprinting, or document understanding. Phases 1–6.5 implemented.
7. [cli_command_reference.md](/c:/Python/Duke/Standalone/docs/cli_command_reference.md) — use §15g for the current LLM extraction, blocker-reduction, and guarded promotion loop.

Use [agent_workflows.json](/c:/Python/Duke/Standalone/docs/agent_workflows.json) and
[agent_tool_registry.json](/c:/Python/Duke/Standalone/docs/agent_tool_registry.json)
as the default command/workflow source of truth.

- **Session 46+ (2026-05-01):** Phase 6.5 autonomous loop implemented:
  - 5 new CLI commands: `lineage deduplicate-documents-nc`, `lineage backfill-evidence-nc`, `lineage backfill-content-hash-nc`, `recommend-missing-dockets-nc`, `run-autonomous-cycle-nc`
  - New module: `action_registry.py` — maps 7 finding categories to corrective commands with severity thresholds, risk assessment, safety caps
  - `run-autonomous-cycle-nc` — full detect→decide→act→measure loop. Safe by default (`--dry-run`).
  - Reprocess pipeline fix: `_refresh_historical_artifacts_for_reprocess` now runs `classify_span_against_families()` between segment and save (was root cause of 290 docs without evidence_json)
  - `run-overnight-db-intelligence-nc` now includes action recommendations from the registry
  - Evidence coverage: 39.1% → 75.6% (+174 docs) after pipeline fix + queue drain
  - All corrective commands follow `--dry-run`/`--execute` pattern for autonomous safety
- **Session 45 (2026-05-01):** Phases 2.5–5.5 of document_intelligence_roadmap completed:
  - Phase 2.5: Ollama orchestration layer (check-ollama-models-nc, run-llm-doc-probe-nc)
  - Phase 3: 11 multi-dimensional flag classifiers (backfill-flag-classifications-nc)
  - Phase 4: Embedding similarity classifier with KNN cosine similarity (embed-corpus-nc, backfill-embedding-classifications-nc)
  - Phase 5: LLM adjudication for rule/embedding disagreements (adjudicate-classifications-nc)
  - Phase 5.5: Overnight document intelligence loop (run-overnight-doc-intelligence-nc)
  - DB now has: 100 document_embeddings rows, 20 embedding_knn_v1 classifications, 5 llm_qwen3:8b_v1 adjudications
  - 3-way classification comparison works: rule → embedding → LLM for document_type stage

## Current State

Operational metrics from `python -m duke_rates show-workflow-status-nc` after the
2026-05-12 overnight backlog-drain run:

| Metric | Value |
|---|---|
| NC historical_docs | 928 |
| NC linked tariff_versions | 1,191 |
| NC versions with charges | 728/1,191 (61.1%) |
| Reprocess queue | 0 pending, 5 stale `running` |
| Stale historical docs | 0 |
| Never processed | 202 |
| Provisional families | 139 |
| Null `effective_start` | 413 |
| OCR queue | 10 pending, 0 running after smoke reset |

Interpretation:
- The overnight run improved charge coverage from `60.1%` to `61.1%` and drained
  stale historical docs from `33` to `0`.
- The old repeated Tesseract OCR loop was caused by remediation reporting not
  recognizing completed OCR artifacts when the artifact hash did not match
  `historical_documents.content_hash`. The report now joins latest OCR artifacts
  by `source_pdf`, so completed OCR should not be re-enqueued just because the
  stored hash differs.
- `null_effective_start=413` is not a bootstrap problem. Run
  `workflow remediate-nc-missing-doc-effective-start`, then re-check promotion blockers.
- The 5 `reprocess_running` rows are stale rows from 2026-05-01 through
  2026-05-04. Inspect with `reprocess show-stale-nc` and recover them with
  `reprocess recover-stale-nc --execute` if they are still stuck.
- LLM extraction is currently idle because the remaining stage-1 candidate pool
  is dominated by parser/routing blockers (`regex_gap`, `wrong_profile`) rather
  than rows ready for extraction.

### 2026-05-12 LLM Extraction / Promotion Status

The current highest-value long-run workflow is **LLM extraction + deterministic validation/repair + guarded promotion**, not regex synthesis.

Recent short-loop observations:
- A bounded `10`-minute parse-improvement test processed `10` docs and produced `44` staged candidate rate rows.
- Deterministic validation accepted `42` of those rows.
- The guarded promotion runner now creates proposals for newly validated rows before refreshing existing proposals.
- Current proposal gate after the latest repair pass:
  - `31` pending promotable proposals
  - promotion dry-run: `31 evaluated`, `31 would promote`, `0 skipped`
  - no production charges are inserted unless `--execute-safe` is explicitly used.
- Recent blocker reductions:
  - `missing_version_effective_start` fell from a broad backlog to only a few residual cases after safe rerouting through same-family dated versions.
  - broad multi-number summary/table lines are now held by `ambiguous_numeric_table_row` instead of being auto-promotable.
  - future Leaf 601 BA component rows can use rider-summary line dates when the match is unique and a dated target version already exists.

## Immediate Focus

1. **Priority 30 (OCR):** 132 Tesseract candidates processed ✓. Remaining: 94 Docling structure lane + 16 GLM review.
   - Next: `OLLAMA_HOST="http://127.0.0.1:1" python -m duke_rates process-docling-batch --ocr-remediation --source historical --workers 1`
   
2. **Priority 35 (Bootstrap):** 21 never-processed docs. Previous run was a no-op (all already had versions).

3. **Priority 60 (Parser):** Additional unmapped families:
   - Riders needing profiles: PS (49 docs), RIDERLC (33 docs)
   - Schedules: PPBE (23 docs), S (78 docs — Schedule S Unmetered Signs)
   - PROSPECTIVERIDER (66 docs) — misclassified schedule pages from compliance bundles
   - Generic_residential empty: leaf-708 (101 docs, 0 charges), leaf-703 (99), leaf-640 (92), leaf-658 (84)
   - All need OCR text + extraction re-run after profile registration.

## Session Start Commands

Run these first:

```powershell
python -m duke_rates show-workflow-status-nc
python -m duke_rates show-workflow-next-actions-nc
python -m duke_rates ocr show-remediation-candidates-nc
python -m duke_rates parse-review-summary
python -m duke_rates reprocess show-queue-nc
python -m duke_rates reprocess show-stale-nc
python -m duke_rates reprocess show-stale-historical-nc
```

For document intelligence state (Phases 2.5–6.5):
```powershell
python -m duke_rates check-ollama-models-nc
python -m duke_rates list-document-types-nc
python -m duke_rates report-document-types-nc
python -m duke_rates report-classification-disagreements-nc --cross-stage document_type
python -m duke_rates report-flag-classifications-nc
python -m duke_rates analyze-parse-failures-nc --dry-run --limit 10
python -m duke_rates benchmark-ollama-roles-nc --task parse_diagnosis --limit 5 --max-runtime-minutes 60
python -m duke_rates run-overnight-parse-improvement-nc --dry-run --task-kind diagnose,extract_staged --limit 10
```

Parse-improvement model note:
- `parse_failure_triage` now uses `mistral:7b-instruct` as primary after the
  2026-05-05 fixture-backed `benchmark-ollama-roles-nc --task all` run. It was
  the only tested model with nonzero parse-diagnosis gold accuracy on the first
  9 labeled cases. `gemma4:e4b-it-q4_K_M`, `qwen3:8b`, and `phi3.5:latest`
  remain fallbacks for schema-valid complementary behavior.
- `structured_rate_extraction` now uses `gemma4:e4b-it-q4_K_M` as primary. It
  was the only viable tested extraction model: 100% gold accuracy on valid
  fixture returns and no timeouts in the fixture-backed run.
- `regex_suggestion` now uses `qwen3:8b` as primary. Treat it as a **secondary**
  advisory path, not the default overnight value-creation lane. The more reliable
  current pattern is direct extraction plus deterministic validation and blocker
  remediation. After expanding regex gold
  fixtures from 1 to 2 cases, `qwen3:8b`, `mistral:7b-instruct`, and
  `phi3.5:latest` all reached 100% valid JSON and 100% gold accuracy; `qwen3:8b`
  was the fastest among those fully valid models. `gemma4:e4b-it-q4_K_M`,
  `nemotron-3-nano:4b`, and `ministral-3:8b` each failed schema validation on
  one of two cases. The previous 100% failure was a schema-normalization issue:
  models emitted `risk_level` and string test cases, which `RegexSuggestion` now
  accepts. The prompt now explicitly asks for confidence so future reports
  should not default to 0.0 for valid suggestions.
- Regex gold fixtures now include 2 cases: `diagnosis:55` (Schedule NL formula
  line) and `diagnosis:69` (Residential New Construction incentive/kWh lines).
  The regex benchmark can include explicit fixture diagnosis IDs even if an old
  suggestion already exists, and regex missed-text excerpting now favors
  rate/unit/incentive lines in long documents instead of blindly taking the
  first 1500 characters.
- `smallthinker:latest` is removed from this role because it mostly punted to
  `unknown`/low-confidence labels. `llama3.1:8b-instruct-q4_K_M` failed schema
  validation for this task; `qwen2.5-coder:7b` showed a 20% schema-error rate.
- `hard_parse_diagnosis` still uses `qwen2.5-coder:7b`; benchmark that role
  separately before changing it.
- Important: Ollama role fallback only runs on timeout/HTTP/schema failure. It
  does not act as a quality second opinion if the primary returns valid but
  biased JSON. Add an ensemble/validator mode later if we want model consensus.
- Existing `llm_parse_diagnostics` rows are skipped by the current resume /
  candidate-selection path unless `--rediagnose-unknown` is used. Re-diagnosis
  is append-only: it creates a fresh diagnostic row for prior `unknown` or
  confidence 0.0 rows without deleting the old result.
- 2026-05-05 follow-up: the overnight parse-improvement test showed candidates
  were often missing usable text. `parse_diagnosis.py` and
  `schema_extraction.py` now require a resolved `historical_document_id` plus
  page-artifact or `raw_text_path` text, and both fall back to
  `historical_documents.raw_text_path`. A 2-doc live diagnosis after the fix
  produced actionable `regex_gap` and `no_rate_table` results.
- `benchmark-ollama-roles-nc` is the sanctioned replacement for
  `tmp_model_benchmark.py`. It benchmarks configured or explicit local Ollama
  models against production-style prompts/schemas for parse diagnosis, hard
  diagnosis, regex suggestion, structured extraction, and document
  classification. Reports are written under
  `docs/reports/ollama_model_benchmarks/` and do not mutate DB rows.
- Use `--task all` or a comma-separated task list when evaluating specialist
  model layering. Multi-task reports include per-task rankings, label-bias
  scores, diversity counts, and model-pair disagreement rates.
- Use `--fixtures docs/reports/ollama_model_benchmarks/gold_fixtures.json`
  for accuracy scoring. The first frontier-reviewed fixture set has 20 cases:
  9 parse diagnosis, 1 regex suggestion, 5 structured extraction, and 5
  document classification. Without fixtures the benchmark ranks structure,
  speed, bias, and complementarity, not true accuracy.

For database intelligence / autonomous loop (Phase 6.5+):
```powershell
python -m duke_rates report-database-intelligence-nc --limit 50 --json
python -m duke_rates run-autonomous-cycle-nc --dry-run --json
python -m duke_rates run-continuous-loop-nc --dry-run --max-cycles 3 --sleep 1
python -m duke_rates lineage backfill-content-hash-nc --dry-run
python -m duke_rates lineage deduplicate-documents-nc --dry-run
python -m duke_rates lineage backfill-evidence-nc --dry-run
python -m duke_rates recommend-missing-dockets-nc --json
```
1
For continuing OCR backlog processing (canonical path):
```powershell
python -m duke_rates ocr process-backlog-nc --workers 4
```
The super-command runs enqueue → drain (`--until-empty`) → extract in sequence. Use
`--skip-enqueue` or `--skip-extract` to run partial phases. For the structure-sensitive
lane, follow up with `process-docling-batch --ocr-remediation --source historical`.

For other priorities:
```powershell
python -m duke_rates bootstrap-missing-versions-nc && python -m duke_rates extract-rates-nc
python -m duke_rates reprocess enqueue-stale-nc --limit 23
```

If reports need regeneration:

```powershell
python -m duke_rates export nc-coverage-assessment
python -m duke_rates export nc-anomaly-audit
python -m duke_rates export nc-schedule-inventory-audit
python -m duke_rates export dep-compliance-bundle-audit
python -m duke_rates export dep-storm-history-inventory
```

## Current Guardrails

- Prefer sanctioned CLI workflows over one-off scripts.
- Prefer improving the existing tool or workflow if a repeated manual step is found.
- Use targeted queueing and bounded registration/reprocessing instead of broad reruns.
- Treat `mine-ncuc-pipeline` as a compatibility alias, not the preferred intake command.
- Keep long session history, investigations, and evidence in `docs/reports/`, not in this file.
- For document intelligence: rule first → embedding second → LLM third. LLM labels are not load-bearing without Phase 6 human review.
- Embedding generation requires Ollama running on `localhost:11434` with `qwen3-embedding:0.6b`. LLM adjudication requires `qwen3:8b`.

## Document Intelligence Next Steps

- **Corpus maintenance (autonomous):** `python -m duke_rates run-autonomous-cycle-nc --execute --max-actions 2` — detect duplicates, missing versions, stale artifacts and apply corrective actions.
- **Continuous loop (8-24h unattended):** `python -m duke_rates run-continuous-loop-nc --execute --max-runtime 480 --max-cycles 20` — runs the full detect→decide→act→acquire→measure loop with docket fetching. Stops when runtime expires, cycles exhausted, or no improvement for 2 cycles. Dry-run first: `run-continuous-loop-nc --dry-run --max-cycles 3 --sleep 1`.
- **Overnight report + action:** `python -m duke_rates run-overnight-db-intelligence-nc --limit 100 --max-runtime 120` then `python -m duke_rates run-autonomous-cycle-nc --execute --max-actions 3`. Morning report JSON at `docs/reports/database_intelligence/<date>_morning.json`.
- **Deduplication:** `python -m duke_rates lineage deduplicate-documents-nc --dry-run` first. There are 50+ duplicate groups (max 42 copies of one hash). Consolidation preserves charges via FK remapping.
- **Docket coverage:** `python -m duke_rates recommend-missing-dockets-nc --json` shows highest-value dockets to fetch next.
- **Phase 5.6 fresh diagnosis run:** `python -m duke_rates run-overnight-parse-improvement-nc --task-kind diagnose --limit 25 --max-runtime-minutes 120 --resume`
- **Phase 5.6 re-diagnose prior unknowns:** `python -m duke_rates run-overnight-parse-improvement-nc --task-kind diagnose --rediagnose-unknown --limit 25 --max-runtime-minutes 120`
- **Phase 5.6 model benchmark:** `python -m duke_rates benchmark-ollama-roles-nc --task parse_diagnosis --models gemma4:e4b-it-q4_K_M,qwen3:8b,mistral:7b-instruct,phi3.5:latest --limit 10 --max-runtime-minutes 90`
- **Phase 5.6 specialization benchmark:** `python -m duke_rates benchmark-ollama-roles-nc --task all --models gemma4:e4b-it-q4_K_M,qwen3:8b,mistral:7b-instruct,phi3.5:latest --limit 5 --timeout-s 120 --max-runtime-minutes 360`
- **Phase 5.6 staged follow-up:** use regex suggestion/validation selectively for parser research, not as the main production backlog reducer. Default to `extract_staged` plus the deterministic promotion loop below.
- **Phase 6 (review queue):** New `classification_reviews` table, `review-queue-nc` CLI, `export-training-dataset-nc`. Turns reviewed labels into training data for future ML classifiers.

## Recommended LLM Extraction Overnight Workflow

Use this when the goal is to reduce uncharged-document backlog and convert extracted rows into safe promotion candidates.

### Preflight: Do Not Start Blind

Run this before a long overnight loop:

```powershell
python -m duke_rates show-workflow-status-nc
python -m duke_rates ocr show-queue-nc --status all --limit 10
python -m duke_rates ocr show-remediation-candidates-nc --limit 25
python -m duke_rates reprocess show-queue-nc --status running --limit 10
python -m duke_rates show-llm-row-effective-status-nc --json
python -m duke_rates propose-llm-charge-promotions-nc --limit 10000 --refresh-existing --json
```

Decision rules:
- If OCR candidates are mostly `run_docling_or_paddle_structure`, do not spend
  the night on repeated Tesseract. Run the Docling/Paddle lane or parser review.
- If bootstrap reports `Historical docs missing versions: 0`, do not run full
  `extract-rates-nc` repeatedly just because `null_effective_start` is nonzero.
- If LLM extraction reports `filtered_at_stage_1` with unchanged
  `regex_gap`/`wrong_profile`, switch to parser-profile/routing work instead of
  another extraction loop.
- If promotion blockers are mostly `unqualified_rate_unit` or
  `unsupported_charge_type`, run focused evidence/mapping repair passes before
  another broad LLM extraction pass.

### A. Start With a Baseline

```powershell
python -m duke_rates show-llm-row-effective-status-nc --json
python -m duke_rates propose-llm-charge-promotions-nc --limit 10000 --refresh-existing --json
python -m duke_rates promote-llm-charge-proposals-nc --limit 500 --json
```

Interpretation:
- `show-llm-row-effective-status-nc` tells you whether extraction quality is improving.
- `propose-llm-charge-promotions-nc --refresh-existing` shows current blocker mix without writing new proposals.
- `promote-llm-charge-proposals-nc` is a dry-run unless `--execute` is added.

### B. Preferred Iterative Short Loop

Use `10`-minute bounded loops while tuning prompts/models or measuring whether a blocker-remediation change is helping:

```powershell
python -m duke_rates run-overnight-parse-improvement-nc `
  --task-kind diagnose,extract_staged `
  --max-runtime-minutes 10 `
  --limit 10 `
  --resume `
  --auto-rediagnose-unknown

python -m duke_rates run-llm-promotion-overnight-nc `
  --validation-limit 500 `
  --repair-limit 1000 `
  --proposal-limit 10000 `
  --promotion-limit 500 `
  --json
```

Read the second report before changing code:
- Did validated row count increase?
- Did `proposal_create` find new rows?
- Did pending promotable rows rise?
- Did blocker counts move in the intended direction?

### B2. One-Hour Targeted Loop

Use this when you want to test whether the LLM lane can improve the blocker
mix without spending the whole night on it. The point is to focus on the two
profiles that still produce useful candidates, then immediately push those rows
through validation and repair.

If you want a reusable launcher instead of manual copy/paste, use
[`scripts/overnight/targeted_llm_blocker_loop.ps1`](/c:/Python/Duke/Standalone/scripts/overnight/targeted_llm_blocker_loop.ps1).

```powershell
# 0. Baseline and routing diagnostics
python -m duke_rates show-workflow-status-nc
python -m duke_rates show-parser-improvement-candidates-nc --limit 25
python -m duke_rates show-near-miss-profiles-nc --limit 25
python -m duke_rates show-unknown-routing-audit-nc --limit 25

# 1. Requeue routing-impact docs and drain the queue
python -m duke_rates reprocess show-stale-nc --limit 10
python -m duke_rates reprocess recover-stale-nc --limit 10 --older-than-minutes 240 --execute
python -m duke_rates reprocess enqueue-profile-impact-nc --parser-profile progress_single_value_rider --limit 25 --requested-by targeted_llm_blocker_loop
python -m duke_rates reprocess enqueue-profile-impact-nc --parser-profile generic_residential --limit 25 --requested-by targeted_llm_blocker_loop
python -m duke_rates reprocess enqueue-profile-impact-nc --parser-profile zero_charge_program --limit 25 --requested-by targeted_llm_blocker_loop
python -m duke_rates reprocess enqueue-profile-impact-nc --parser-profile progress_current_leaf_bridge --limit 25 --requested-by targeted_llm_blocker_loop
python -m duke_rates reprocess process-queue-nc --limit 25 --workers 4

# 2. Extract from the two highest-value near-miss profiles
python -m duke_rates run-overnight-parse-improvement-nc --task-kind extract_staged --max-runtime-minutes 15 --limit 10 --resume --auto-rediagnose-unknown --profile progress_single_value_rider
python -m duke_rates run-overnight-parse-improvement-nc --task-kind extract_staged --max-runtime-minutes 15 --limit 10 --resume --auto-rediagnose-unknown --profile generic_residential

# 3. Convert candidate rows into actionable validation / repair state
python -m duke_rates validate-llm-rate-extractions-nc --limit 200 --execute
python -m duke_rates locate-llm-row-evidence-nc --issue unit_missing --limit 50 --execute
python -m duke_rates reclassify-llm-row-conflicts-nc --limit 50 --execute
python -m duke_rates apply-deterministic-llm-row-repairs-nc --limit 200 --execute

# 4. Refresh promotion state and inspect whether anything became promotable
python -m duke_rates propose-llm-charge-promotions-nc --limit 10000 --refresh-existing --execute --json
python -m duke_rates promote-llm-charge-proposals-nc --limit 500 --execute --json
python -m duke_rates show-llm-row-effective-status-nc --json
python -m duke_rates show-workflow-status-nc
```

Stop early if:
- the routing diagnostics keep surfacing the same top families with no enqueue impact
- the extraction passes report only `skip` / `filtered_at_stage_1`
- validation mostly returns `no_rate_rows`
- evidence-location mostly returns `unsupported_unit` or `evidence_quote_missing`
- promotion still evaluates to `0 promotable` after deterministic cleanup

Interpretation:
- If `generic_residential` or `progress_single_value_rider` produce new
  `validated` rows but promotions still block, the remaining problem is unit /
  charge-type / table-ambiguity cleanup, not extraction volume.
- If both extraction passes are mostly idle, stop the extraction lane and stay
  on routing / reprocess work instead.

### C2. Routing-First Overnight Until 9am

Use this when the backlog is still dominated by `unknown_profile` routing
gaps and reprocess work. It is the best overnight loop when the main objective
is to reduce backlog rather than to keep broad extraction lanes busy.

For a reusable launcher, use
[`scripts/overnight/routing_first_until_9am.ps1`](/c:/Python/Duke/Standalone/scripts/overnight/routing_first_until_9am.ps1).

```powershell
pwsh scripts\overnight\routing_first_until_9am.ps1 -DeadlineTime "09:00"
```

This loop:
- inspects `show-unknown-routing-audit-nc` each cycle
- enqueues impacted docs for synthesized existing profiles such as
  `progress_recovery_rider`, `zero_charge_program`, and
  `progress_billing_adjustments`
- drains `reprocess process-queue-nc --until-empty`
- re-measures workflow status after each cycle

Stop early if:
- the unknown-routing audit keeps returning the same families with no new
  profile-impact enqueues
- the reprocess queue is empty and no new impacted profiles are being found

### C. Full Overnight Pattern

When the short loop is productive, scale the exact same workflow:

```powershell
python -m duke_rates run-overnight-parse-improvement-nc `
  --task-kind diagnose,extract_staged `
  --max-runtime-minutes 360 `
  --limit 100 `
  --resume `
  --auto-rediagnose-unknown

python -m duke_rates run-llm-promotion-overnight-nc `
  --validation-limit 2000 `
  --repair-limit 4000 `
  --proposal-limit 20000 `
  --promotion-limit 1000 `
  --json
```

Do **not** assume this should execute promotions immediately. First inspect:
- `pending_promotable`
- `promotion_dry_run.skipped`
- the largest `pending_blockers`

### C2. Multi-Phase Backlog-Drain Wrapper

The wrapper is useful when OCR, stale reprocess, bootstrap, and LLM promotion all
have real work. It is not the best tool when only one lane is active.

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

Morning checks:

```powershell
python -m duke_rates show-workflow-status-nc
python -m duke_rates ocr show-remediation-candidates-nc --limit 25
python -m duke_rates ocr report-benchmark-nc --limit 50
python -m duke_rates reprocess show-queue-nc --status running --limit 10
python -m duke_rates propose-llm-charge-promotions-nc --limit 10000 --refresh-existing --json
python -m duke_rates promote-llm-charge-proposals-nc --limit 500 --json
```

Stop conditions:
- Stop or retarget if the same OCR families reappear after completed OCR.
- Stop or retarget if `bootstrap-missing-versions-nc` creates zero rows.
- Stop or retarget if LLM extraction is idle for two consecutive passes.
- Stop before `--execute-safe` if dry-run promotions show any skipped rows.

### D. Safe Promotion Decision

Only use safe execution after a dry-run shows clean promotable rows:

```powershell
python -m duke_rates run-llm-promotion-overnight-nc `
  --validation-limit 2000 `
  --repair-limit 4000 `
  --proposal-limit 20000 `
  --promotion-limit 250 `
  --execute-safe `
  --json
```

Current rule of thumb:
- promote when the dry-run shows `skipped = 0`
- pause if promotable rows are dominated by new semantic edge cases
- add deterministic blockers/repairs before execution if the rows look structurally suspicious

### E. Backlog-Reduction Heuristics

Work blocker buckets in this order:

1. `missing_version_effective_start`
   - Often reducible with deterministic rerouting to an existing dated sibling version.
   - Leaf 601 BA-like rider rows can also use unique rider-summary line dates when available.
2. `malformed_family_key`
   - Canonical reroute only when an exact dated canonical version already exists.
3. `unqualified_rate_unit`
   - Prefer deterministic unit evidence and focused evidence-location passes.
4. `unsupported_charge_type`
   - Expand mappings only when text patterns are stable and auditable.
5. `ambiguous_numeric_table_row`
   - Treat as a hold/review class, not an execution target, until a precise table-column locator exists.

The goal is to move work through this funnel:

```text
extract_staged
-> validate rows
-> deterministic repairs
-> propose promotions
-> review blocker distribution
-> dry-run promotion
-> execute-safe only when clean
```

## Where Historical Detail Lives

Use these when you need supporting detail rather than current routing:

- [docs/reports/README.md](/c:/Python/Duke/Standalone/docs/reports/README.md)
- [roadmap.md](/c:/Python/Duke/Standalone/docs/roadmap.md)
- [document_intelligence_architecture.md](/c:/Python/Duke/Standalone/docs/document_intelligence_architecture.md)
- [ncuc_pipeline_overview.md](/c:/Python/Duke/Standalone/docs/ncuc_pipeline_overview.md)

This file is intentionally short. Historical accomplishment logs, long caveat lists,
and exploratory notes should be kept in reports or other canonical docs instead.

**Status:** Active
**Update Trigger:** Refresh when current metrics, blockers, or the first-read path materially changes.
