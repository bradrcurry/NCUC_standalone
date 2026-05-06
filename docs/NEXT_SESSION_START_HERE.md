# Next Session: Start Here
**Date Updated:** 2026-05-01 (Session 46+ — Phase 6.5 autonomous loop implemented)
**Purpose:** Short operational handoff with current state, immediate priorities, and the correct entry docs

## Read First

Read these before broad repo exploration:

1. [AGENT_ONBOARDING.md](/c:/Python/Duke/Standalone/AGENT_ONBOARDING.md)
2. [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md)
3. [agent_tool_use_policy.md](/c:/Python/Duke/Standalone/docs/agent_tool_use_policy.md)
4. [document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)
5. [NEXT_SESSION_PRIORITIES.md](/c:/Python/Duke/Standalone/docs/NEXT_SESSION_PRIORITIES.md)
6. [document_intelligence_roadmap.md](/c:/Python/Duke/Standalone/docs/document_intelligence_roadmap.md) — read before extending classification, fingerprinting, or document understanding. Phases 1–6.5 implemented.
7. [cli_command_reference.md](/c:/Python/Duke/Standalone/docs/cli_command_reference.md) — **§16 (Database Intelligence) added this session.** Covers 5 new corrective tools + autonomous loop controller.

Use [agent_workflows.json](/c:/Python/Duke/Standalone/docs/agent_workflows.json) and
[agent_tool_registry.json](/c:/Python/Duke/Standalone/docs/agent_tool_registry.json)
as the default command/workflow source of truth.

- **Session 46+ (2026-05-01):** Phase 6.5 autonomous loop implemented:
  - 5 new CLI commands: `deduplicate-documents-nc`, `backfill-evidence-nc`, `backfill-content-hash-nc`, `recommend-missing-dockets-nc`, `run-autonomous-cycle-nc`
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

Operational metrics from `python -m duke_rates show-workflow-status-nc`:

| Metric | Value |
|---|---|
| NC historical_docs | 476 |
| NC linked tariff_versions | 849 |
| NC versions with charges | 639/849 (75.3%) |
| tariff_charges | 14,999 |
| Reprocess queue | 0 pending ✅ |
| Stale historical docs | 0 ✅ |
| Evidence coverage | 360/476 (75.6%) |
| Never processed | 21 |
| Provisional families | 1 |
| Null `effective_start` | 89 |
| OCR pending | 0 ✅ |

Interpretation:
- **Session 44 completed** — profile registrations + OCR drain + extraction.
- **107 OCR Tesseract candidates processed** (0 failures). Ollama 500 errors blocked by setting `OLLAMA_HOST` to dummy address.
- **Profile changes took effect in extraction:** OPT-E → 28 charges, OPT-V → 20, OPT-G/H/I → 11/6/6, BC → 28, I → 103, TS → 27, WC → 6, leaf-607 → 87.
- **HP, NL still need OCR text** — they're in the Docling structure-sensitive lane (94 candidates).
- **Extraction ran 4/27 16:29-17:53 ET** with Ollama disabled, completing all 757 docs in ~84 min (vs ~6h estimated with broken Ollama).

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
python -m duke_rates show-ocr-remediation-candidates-nc
python -m duke_rates parse-review-summary
python -m duke_rates show-reprocess-queue-nc
python -m duke_rates show-stale-historical-nc
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
python -m duke_rates run-overnight-parse-improvement-nc --dry-run --task-kind diagnose,suggest,validate,extract --limit 10
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
- `regex_suggestion` now uses `qwen3:8b` as primary. After expanding regex gold
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
python -m duke_rates backfill-content-hash-nc --dry-run
python -m duke_rates deduplicate-documents-nc --dry-run
python -m duke_rates backfill-evidence-nc --dry-run
python -m duke_rates recommend-missing-dockets-nc --json
```
1
For continuing OCR backlog processing (canonical path):
```powershell
python -m duke_rates process-ocr-backlog-nc --workers 4
```
The super-command runs enqueue → drain (`--until-empty`) → extract in sequence. Use
`--skip-enqueue` or `--skip-extract` to run partial phases. For the structure-sensitive
lane, follow up with `process-docling-batch --ocr-remediation --source historical`.

For other priorities:
```powershell
python -m duke_rates bootstrap-missing-versions-nc && python -m duke_rates extract-rates-nc
python -m duke_rates enqueue-stale-reprocess-nc --limit 23
```

If reports need regeneration:

```powershell
python -m duke_rates export-nc-coverage-assessment
python -m duke_rates export-nc-anomaly-audit
python -m duke_rates export-nc-schedule-inventory-audit
python -m duke_rates export-dep-compliance-bundle-audit
python -m duke_rates export-dep-storm-history-inventory
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
- **Deduplication:** `python -m duke_rates deduplicate-documents-nc --dry-run` first. There are 50+ duplicate groups (max 42 copies of one hash). Consolidation preserves charges via FK remapping.
- **Docket coverage:** `python -m duke_rates recommend-missing-dockets-nc --json` shows highest-value dockets to fetch next.
- **Phase 5.6 fresh diagnosis run:** `python -m duke_rates run-overnight-parse-improvement-nc --task-kind diagnose --limit 25 --max-runtime-minutes 120 --resume`
- **Phase 5.6 re-diagnose prior unknowns:** `python -m duke_rates run-overnight-parse-improvement-nc --task-kind diagnose --rediagnose-unknown --limit 25 --max-runtime-minutes 120`
- **Phase 5.6 model benchmark:** `python -m duke_rates benchmark-ollama-roles-nc --task parse_diagnosis --models gemma4:e4b-it-q4_K_M,qwen3:8b,mistral:7b-instruct,phi3.5:latest --limit 10 --max-runtime-minutes 90`
- **Phase 5.6 specialization benchmark:** `python -m duke_rates benchmark-ollama-roles-nc --task all --models gemma4:e4b-it-q4_K_M,qwen3:8b,mistral:7b-instruct,phi3.5:latest --limit 5 --timeout-s 120 --max-runtime-minutes 360`
- **Phase 5.6 staged follow-up:** after diagnosis produces `regex_gap`, run `python -m duke_rates run-overnight-parse-improvement-nc --task-kind suggest,validate --limit 25 --max-runtime-minutes 120 --resume`. Run `extract` separately; it is the heaviest Ollama stage.
- **Phase 6 (review queue):** New `classification_reviews` table, `review-queue-nc` CLI, `export-training-dataset-nc`. Turns reviewed labels into training data for future ML classifiers.

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
