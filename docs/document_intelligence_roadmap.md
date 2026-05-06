# Document Intelligence Roadmap

**Status:** Living document. Updated as phases land.
**Last Updated:** 2026-05-01 (Phase 5.6 + 6.5 implemented)
**Audience:** Future agents and operators continuing the document-intelligence buildout

> **2026-05-01 update:** Added Phase 5.6 (LLM-assisted parse diagnosis and
> regex improvement loop) — 6 new modules, 5 CLI commands, 4 DB tables,
> 7 Ollama roles. Parse failure diagnosis, regex/normalization suggestion
> generation, deterministic validation harness, schema-guided LLM fallback
> extraction, and an overnight improvement loop — all advisory, never
> auto-modifying parser code.
>
> **2026-04-30 update:** Added Phase 2.5 (Ollama model orchestration layer) and
> Phase 5.5 (Overnight document intelligence loop). Updated Phase 4 embedding
> model recommendations to `qwen3-embedding:0.6b` + `snowflake-arctic-embed2`
> (with `nomic-embed-text` retained as a baseline). Updated Phase 5 model list
> to reflect the current local Ollama inventory. No previously-shipped phase
> has been re-marked complete; Phase 1 still requires corpus validation.

## Why this document exists

The classification, fingerprinting, OCR, and parsing systems in this repo
were built incrementally over many sessions. New agents arriving at this
work consistently propose to rebuild components that already exist —
`document_intelligence/` package, fingerprint tables, OCR routing, parse
review queues. This roadmap captures:

1. **What already exists** and should not be rebuilt
2. **Where this work is going** as a phased, reviewable progression
3. **What the next phase actually requires** — so a session can pick up the
   smallest useful increment without committing to the whole arc

If you are starting a session and want to extend document understanding,
start here, **then** read the canonical entry docs
([AGENT_ONBOARDING.md](/c:/Python/Duke/Standalone/AGENT_ONBOARDING.md),
[document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md),
[document_intelligence_architecture.md](/c:/Python/Duke/Standalone/docs/document_intelligence_architecture.md)).

## Long-term goal

Identify, fingerprint, classify, and route NCUC docket documents reliably
across many document types — not just rate/rider tariff sheets — while
collecting high-quality reviewed labels that can eventually train an
NCUC-specific classifier.

The pipeline shape we are working toward:

```
raw file
  → file registration / provenance
  → file fingerprinting (hard + semantic)
  → OCR / text / layout extraction
  → deterministic metadata extraction
  → multi-stage classification:
       1. rule-based (fast, explainable)
       2. embedding similarity (against reviewed examples)
       3. optional local LLM/VLM adjudication
  → multi-dimensional labels (category + type + role + flags)
  → confidence scoring with rule/embedding/LLM components recorded separately
  → human review queue for low-confidence / high-value / unknown
  → persisted reviewed labels
  → training dataset export
```

## Core principles (do not violate without an explicit decision)

1. **Additive, never destructive.** New tables, new stages — do not migrate
   or delete existing ones. The current pipeline ships ~15,000 tariff_charges
   to production users; breaking it for a redesign is not acceptable.
2. **Deterministic evidence over LLM guesses.** Rules first; embeddings
   second; LLMs only as adjudicators. Every classifier output records its
   evidence so a human can audit it.
3. **Store unknowns instead of forcing labels.** A document that doesn't
   match any known type should land as `UNKNOWN`, get fingerprinted, and
   show up in cluster reports — that's how new types get discovered.
4. **One stage, one record.** Multi-dimensional classification is achieved
   by recording one row per stage in `document_classifications`, not by
   stuffing many fields into one row.
5. **Confidence is a 0..1 scalar plus evidence + alternatives.** Never bare
   labels without a confidence signal. The `ClassificationResult` type is
   the canonical shape.
6. **Validate each phase before adding the next.** Phases are sized so each
   one ships value standalone. A new agent should only need to deliver
   their current phase before stopping for review.

## What already exists (do NOT rebuild)

| Concern | Where it lives | Notes |
|---|---|---|
| Page artifact caching | `ncuc_page_artifacts` table, `pipeline/page_miner.py` | Used by extraction; do not duplicate. |
| Span segmentation | `pipeline/segmentation.py`, `ncuc_span_artifacts` table | Page→span grouping with leaf/code detection. |
| Family matching (rules) | `pipeline/family_matcher.py` `score_span_against_family` | Now wraps `classify_span_against_families` — see Phase 1. |
| OCR backend routing | `pipeline/ocr.py` `select_ocr_backend`, progressive escalation | Decision matrix already exists. Do not write a new router. |
| OCR queue + drain | `db/ocr_queue.py`, CLI `process-ocr-queue-nc` | Race-free claim, single-conn-per-item. |
| Docling chunked conversion | `pipeline/docling_backend.py` `convert_pdf_safe` | OOM-safe with degradation ladder, GLM-OCR last-resort. |
| Document representation | `document_intelligence/representation.py` | Pages, blocks, tables. Used by normalization router. |
| Normalization routing | `document_intelligence/normalization.py` `DocumentNormalizationRouter` | Native PDF / Paddle / GLM-OCR fallback. **Disabled in extract-rates path** — see [bulk_extractor.py:213](src/duke_rates/historical/ncuc/pipeline/bulk_extractor.py#L213). |
| Parser profile registry | `pipeline/parser_profiles.py`, `HistoricalRateParserRegistry` | Rule-based candidate scoring per profile. |
| Parse-attempt logging | `parse_attempt_logs` table | Records ranked candidates, text_metrics, evidence per attempt. |
| Review outcomes | `parse_review_outcomes` table | Existing manual-review surface. |
| Document fingerprints (redline detection) | `document_fingerprints` table | Used by redline analysis. **Don't reuse for general classification.** |
| Document fingerprints v2 (general) | `document_fingerprints_v2` table — Phase 1 | New, broader feature set. |
| Classification observability | `document_classifications` table — Phase 1 | New polymorphic record per classifier decision. |
| Reporting CLIs | `report-ocr-benchmark-nc`, `report-docling-skipped-pages-nc`, `report-classification-disagreements-nc`, `report-document-fingerprint-clusters-nc`, `report-database-intelligence-nc`, `summarize-database-intelligence-nc`, `ask-ncuc-db`, `run-overnight-db-intelligence-nc` | Existing + Phase 1, 6.5 additions. |
| Database intelligence | `database_reports.py`, `db_llm_analysis.py`, `database_intelligence_runs` table — Phase 6.5 | Deterministic SQL reports, LLM summarization, safe NL querying. |

## Phased plan

Each phase is sized to be deliverable in 1-3 focused sessions and produces
something independently useful. **Do not skip phases.** Later phases assume
earlier ones recorded the signals they need.

### Phase 1 — Classification observability ✅ complete (validated 2026-04-30)

**Goal:** every classifier decision is persisted with confidence, evidence,
and runner-up alternatives. Disagreements and low-margin cases are
inspectable.

**Implemented:**

- `ClassificationResult` Pydantic model
  ([src/duke_rates/classification/result.py](/c:/Python/Duke/Standalone/src/duke_rates/classification/result.py))
- `document_classifications` table
  ([src/duke_rates/db/schema.py](/c:/Python/Duke/Standalone/src/duke_rates/db/schema.py))
  — polymorphic on `(subject_kind, subject_id)`, idempotent on
  `(subject_kind, subject_id, stage, classifier, classifier_version)`,
  supports `superseded_by` for second-opinion overlays.
- `record_classification` + `supersede_prior_classifications` helpers
  ([src/duke_rates/classification/persistence.py](/c:/Python/Duke/Standalone/src/duke_rates/classification/persistence.py))
- `document_fingerprints_v2` table — broader features than the existing
  redline-focused `document_fingerprints` table.
- `fingerprint_pdf` / `save_fingerprint`
  ([src/duke_rates/classification/fingerprint.py](/c:/Python/Duke/Standalone/src/duke_rates/classification/fingerprint.py))
  — vocabulary signals, first-page signature, leaf/schedule/rider codes,
  cluster signature.
- Retrofit of family matcher: `classify_span_against_families` returns a
  full `ClassificationResult`; legacy `find_best_family_for_span` is now a
  thin wrapper.
- Importer call sites at
  [importer.py:604](src/duke_rates/historical/ncuc/importer.py#L604) and
  [importer.py:838](src/duke_rates/historical/ncuc/importer.py#L838)
  persist family-mapping classifications and record legacy-hint overrides.
- CLIs:
  - `report-classification-disagreements-nc` — low-margin and override rows
  - `fingerprint-corpus-nc` — bootstrap fingerprints for existing PDFs
  - `report-document-fingerprint-clusters-nc` — group fingerprints by
    cluster signature

**Validation results (2026-04-30):**

- `fingerprint-corpus-nc` ran over the corpus: **8,812 fingerprints across
  4,406 distinct PDFs, 40 clusters with size ≥2.** Top clusters split
  cleanly into three tiers — regulatory boilerplate (DOCKET_HEADER,
  STATE_OF_NC_UC, VIA_ELECTRONIC_FILING; 34–220 docs each), tariff content
  (LEAF_HEADER and tariff-vocab variants; 40–84 docs each), and
  unknown/minimal (scanned-only or text-extraction failures; 36–92 each).
  This cluster output is the seed input for the Phase 2 taxonomy.
- `report-classification-disagreements-nc` runs cleanly and returns 0
  findings — expected, since `family_matcher_v1` is currently the only
  classifier writing rows. The report is wired and will start producing
  signal as soon as Phase 2 adds a second `document_type` classifier.
- `document_classifications` contains **390 rows / 879 historical_documents**
  on stage `family_mapping`. The remaining 489 documents have a
  `family_key` set on `historical_documents` but no classification row —
  the ingest backfill skipped them because their `evidence_json` was
  either NULL (455 docs) or non-numeric (34 docs, e.g.
  `{"schedule_code": "SLS"}`).

**Known caveats carried forward (must not be forgotten in Phase 2):**

1. **All 390 existing rows are `confidence=1.0`, `classifier_version=backfill_v1`.**
   Evidence kinds (`explicit_leaf_hit`, `schedule_code_hit`,
   `heading_alias_similarity`, `tariff_vocab_density`) are preserved and
   useful, but the numeric scores were flattened to 1.0 during backfill.
   The disagreement report's low-margin filter is therefore blind on
   these rows. Phase 2's new `document_type` classifier will produce a
   real confidence distribution; rely on that for the first real
   disagreement signal, not on retroactively re-scoring the family_mapping
   backfill.
2. **489-document family_mapping gap is closed as a Phase 2 side effect,
   not by a dedicated backfill pass.** When Phase 2's ingest path adds a
   `document_type` classification for every document, those same docs
   will also receive fresh `family_mapping` rows from the live classifier
   (not the backfill), with real confidence scores. Do **not** re-run
   `backfill_v1` to "fix" the 489 — that would write more 1.0-confidence
   rows on top of the gap.
3. **Importer call sites currently persist `family_key` without
   simultaneously writing a `document_classifications` row** when the
   scoring evidence is non-numeric. Phase 2 should fix this at the call
   site (live ingest path), not via another backfill, to prevent the gap
   reopening as new documents arrive.

The BA-2122/leaf-653 misclassification documented in the
[Bucket A triage](/c:/Python/Duke/Standalone/docs/reports/zero_charge_triage_2026_04_29.json)
remains a useful smoke-test target once Phase 2's `document_type` stage
is live and producing variable confidences.

**What Phase 1 deliberately does NOT include:**

- Multi-dimensional classification (category / type / role) — Phase 2.
- Embedding-based classifiers — Phase 4.
- LLM second opinion — Phase 5.
- A document_types taxonomy table — Phase 2.
- Review queue UI — Phase 6.
- Training export — Phase 6.

### Phase 2 — Document type taxonomy as data ✅ complete (validated 2026-04-30)

**Goal:** make the document-type taxonomy explicit, queryable, and
extensible. Stop hardcoding `"tariff" / "procedural" / "order"` in
classifier branches.

**Deliverables:**

1. New `document_types` table seeded with a taxonomy. Columns:
   `id, code, primary_category, parent_type, description, is_terminal,
   created_at`. Primary categories from the proposed enum:
   `ORDERS_AND_DECISIONS, APPLICATIONS_AND_PETITIONS,
   TARIFF_AND_RATE_DOCUMENTS, TESTIMONY_AND_EXHIBITS, DISCOVERY,
   SETTLEMENT_DOCUMENTS, PROCEDURAL_AND_ADMINISTRATIVE,
   PUBLIC_NOTICE_AND_COMMENTS, REPORTS_AND_COMPLIANCE,
   NOISE_OR_DUPLICATES`.
2. Document types within each category, e.g. `TARIFF_SHEET`, `RIDER`,
   `RATE_SCHEDULE`, `REVISED_TARIFF_SHEET`, `TARIFF_REDLINE`,
   `ORDER_FINAL`, `ORDER_PROCEDURAL`, `APPLICATION`, `COMPLIANCE_FILING`,
   `TESTIMONY_DIRECT`, `EXHIBIT`, `DISCOVERY_RESPONSE`,
   `SETTLEMENT_AGREEMENT`, `NOTICE_OF_HEARING`, `COVER_LETTER`,
   `CERTIFICATE_OF_SERVICE`, `PUBLIC_COMMENT`, `STAFF_RECOMMENDATION`,
   `DUPLICATE`, `INDEX`, `UNKNOWN`. Seed initially with the 10-15 types
   most common in the corpus; let `report-document-fingerprint-clusters-nc`
   surface the rest before adding them.
3. New stage `document_type` in `document_classifications`. Retrofit the
   existing `classify_document` rule (currently produces
   `tariff/procedural/order/unknown`) to emit a `ClassificationResult` with
   a `document_type` label from the taxonomy.
4. CLI `list-document-types-nc` to show the taxonomy.
5. CLI `report-document-types-nc` to show classification distribution.

**Constraints:**

- Map existing labels (`tariff`, `procedural`, `order`) to the new taxonomy
  for backward compatibility. The old labels should keep working in the
  existing classifier's metadata while a new `document_type` stage is
  added alongside.
- Don't try to classify all 50+ types in one session. Cover the common
  cases; leave the rest as `UNKNOWN`.
- Don't break extraction — the existing `bulk_extractor` path that
  short-circuits on `doc_type != 'tariff'` should keep working with the
  legacy labels until Phase 3 is ready to consume the new taxonomy.

**Definition of done:**

- Running ingest populates `document_classifications` rows with stage
  `document_type` for every new document.
- Running `report-document-types-nc` shows a non-trivial distribution
  across the seeded types (i.e. not 100% UNKNOWN).
- The disagreement report flags cases where rule-based document_type and
  the existing `tariff`/`procedural`/`order` legacy field disagree.

**Validation results (2026-04-30):**

- Seeded `document_types` table with 12 terminal types across 6 primary
  categories. `list-document-types-nc` confirms.
- `DocumentClassifier.classify_with_result()` added in
  [pipeline/document_prep.py](/c:/Python/Duke/Standalone/src/duke_rates/historical/ncuc/pipeline/document_prep.py) —
  preserves the legacy string label byte-for-byte and returns a
  `ClassificationResult` for the new `document_type` stage. The legacy
  `classify()` method is unchanged so the existing extraction
  short-circuit (`doc_type != 'tariff'`) and the per-page scan in
  `_find_tariff_type_in_pages` keep working.
- Wired into the importer's bulk-extraction call site at
  [bulk_extractor.py:875](src/duke_rates/historical/ncuc/pipeline/bulk_extractor.py#L875)
  via the new `_record_document_type_classification` helper — side-effect
  only, never raises, never blocks extraction.
- New CLIs: `list-document-types-nc`, `report-document-types-nc`.
- Backfilled the existing 879 historical_documents one-shot via the new
  classifier; result distribution: ORDER_FINAL 50.3%, TARIFF_SHEET 31.4%,
  COVER_LETTER 9.7%, UNKNOWN 5.1%, TESTIMONY 3.5%. **Not 100% UNKNOWN —
  Phase 2 definition-of-done met.** Confidence distribution spans 0.0 →
  0.7 (no flattening), so the disagreement report's low-margin filter is
  now real.

**Known limitations carried forward (must not be forgotten in Phase 3+):**

1. **ORDER_FINAL is over-counted (~50%).** The legacy classifier's
   `procedural_regexes` fire on bare `"order"` / `"approving"` tokens,
   so many cover letters and notices land under ORDER_FINAL. This is a
   *pre-existing* classifier-quality issue, not a Phase 2 regression.
   Phase 3's `flag_is_final` / `flag_is_proposed` boolean classifiers
   and Phase 5's LLM adjudication are the right places to refine — do
   not patch the legacy regex set in isolation.
2. **Legacy ↔ document_type crosswalk is currently 1:1 deterministic.**
   The disagreement report cannot surface document_type disagreements
   yet because only one classifier writes the stage. Real disagreement
   signal arrives when Phase 4 (embedding) or Phase 5 (LLM) starts
   writing competing rows.
3. **Phase 1 caveat #3 retired.** Re-reading the live importer paths
   (`importer.py:680`, `importer.py:947`) confirmed both already record
   `family_mapping` classifications correctly when the classifier
   returns a result. The 489-doc family_mapping gap was a property of
   the historical `backfill_v1` script, not the live ingest, so no
   call-site fix is needed. The gap will close naturally as documents
   are re-ingested under the live path.

### Phase 2.5 — Ollama model orchestration layer

**Goal:** give every Phase 2+ component a single, declarative way to ask for
an LLM/VLM/embedding model by **role** rather than by hardcoded model name.
Today the only Ollama caller is `GlmOcrNormalizer`, which env-var-reads
`OLLAMA_HOST` and hardcodes `glm-ocr` as the model. That pattern does not
scale to Phases 4–6 where multiple models (embedding, fast classifier,
structured extractor, vision, heavy reasoning) need to coexist with
fallbacks, health probes, timeouts, and per-call evidence persistence.

This phase is **infrastructure-only**: it adds the orchestrator and the
config, but does not change any existing classifier output. Phase 4, 5, 5.5
and 6 consume it.

**Deliverables:**

1. `config/ollama_models.yaml` — declarative role → primary/fallback model
   map. Roles seeded for the current local inventory:

   | Role | Primary | Fallback | Used by |
   |---|---|---|---|
   | `fast_classifier` | `smallthinker:latest` | `qwen3.5:4b` | quick triage / flag classifiers |
   | `balanced_classifier` | `qwen3:8b` | `qwen2.5:7b-instruct` | document_type adjudication |
   | `structured_extractor` | `glm-4.7-flash:latest` | `command-r:latest` | rate-row / metadata JSON extraction |
   | `vision_layout` | `qwen3-vl:4b` | — | page-image classification, layout |
   | `ocr_fallback` | `glm-ocr:latest` | — | already wired via `GlmOcrNormalizer` |
   | `embedding_primary` | `qwen3-embedding:0.6b` | — | Phase 4 |
   | `embedding_secondary` | `snowflake-arctic-embed2:latest` | — | Phase 4 second axis |
   | `legacy_embedding_baseline` | `nomic-embed-text:latest` | — | Phase 4 regression baseline |
   | `heavy_reasoning` | `deepseek-r1:14b` | `gemma4:e4b-it-q4_K_M` | rare hard-case adjudication |
   | `code_model` | `qwen2.5-coder:14b` | `qwen2.5-coder:7b` | dev-side use only |

   YAML shape:

   ```yaml
   defaults:
     host: ${OLLAMA_HOST:-http://localhost:11434}
     request_timeout_s: 120
     json_mode: true
   roles:
     fast_classifier:
       primary: smallthinker:latest
       fallback: [qwen3.5:4b]
       options: { temperature: 0.0 }
       max_tokens: 512
     # ... etc
   prompt_versions:
     # role -> current prompt version string, bumped when prompt text changes
     balanced_classifier: v1
     structured_extractor: v1
   ```

2. `src/duke_rates/document_intelligence/ollama_orchestrator.py` —
   single entry point for all Ollama calls in Phase 2+. Public surface:

   ```python
   class OllamaOrchestrator:
       def __init__(self, config_path: Path | None = None): ...

       def health_probe(self, role: str) -> tuple[bool, str | None]:
           """Probe primary then fallbacks. Cache result for the process lifetime.
           Same fail-fast pattern as GlmOcrNormalizer.is_available()."""

       def list_available_roles(self) -> list[RoleHealth]: ...

       def generate_json(
           self,
           role: str,
           prompt: str,
           schema: type[BaseModel],
           *,
           subject_kind: str,
           subject_id: int | str,
           stage: str,
       ) -> OllamaRunResult:
           """Call the role's model in JSON mode. Validate against `schema`.
           Persist to ollama_model_runs. Returns the parsed model + run metadata.
           Never raises on validation failure — returns a result with
           status='validation_error' and the raw payload for review."""

       def embed(self, role: str, text: str) -> list[float]: ...

       def generate_text(self, role: str, prompt: str) -> OllamaRunResult: ...
   ```

   Constraints:

   - Reuses `GlmOcrNormalizer`-style env-var resolution for `OLLAMA_HOST`.
   - JSON mode (`format=json`) is the default for all `generate_json` calls.
   - On primary failure (timeout, HTTP error, malformed JSON, schema
     validation error), automatically tries the next fallback **once**,
     then gives up and records the failure. No silent retries.
   - The orchestrator does NOT decide what to do with the result — it
     returns it. Callers (Phase 4/5 classifiers) decide how to record it
     into `document_classifications`.

3. New table `ollama_model_runs` (additive — does not change existing
   tables). Columns:

   ```
   id INTEGER PRIMARY KEY
   subject_kind TEXT NOT NULL          -- e.g. 'pdf', 'span', 'page'
   subject_id  TEXT NOT NULL
   stage       TEXT NOT NULL           -- e.g. 'document_type', 'rate_row_extraction'
   role        TEXT NOT NULL           -- 'balanced_classifier' etc
   model       TEXT NOT NULL           -- resolved model name actually called
   prompt_version TEXT NOT NULL
   status      TEXT NOT NULL           -- 'ok' | 'http_error' | 'timeout'
                                       -- | 'json_parse_error' | 'validation_error'
                                       -- | 'fallback_used'
   duration_ms INTEGER
   tokens_in   INTEGER
   tokens_out  INTEGER
   raw_payload TEXT                    -- truncated to ~32 KB
   validation_error TEXT
   created_at  TEXT NOT NULL DEFAULT (datetime('now'))
   ```

   Indexed on `(subject_kind, subject_id, stage)` and `(role, status,
   created_at)` for the overnight loop's resume/skip logic.

4. New CLI `check-ollama-models-nc`:
   - For each role in `ollama_models.yaml`, probe primary + fallbacks,
     run a tiny canned prompt, validate JSON shape (where applicable),
     print a status table.
   - Exits non-zero if any required role has no working model — used as
     a precondition by `run-overnight-doc-intelligence-nc`.

5. New CLI `run-llm-doc-probe-nc`:
   - Take one document (by id or path) and run the structured-extractor
     role against it with the canonical rate-row schema (Phase 5 schema).
   - Prints the model output, validation result, and the row that would
     be persisted. Does **not** write to `document_classifications`
     unless `--persist` is passed.
   - This is the manual smoke-test entrypoint for Phase 5 prompt work
     before any batch run.

**Constraints (apply to everything Phase 2.5 enables):**

- LLM output is **evidence, not truth.** Every persisted row in
  `document_classifications` produced via the orchestrator must carry
  the `ollama_model_runs.id` in its `metadata` so the raw payload is
  traceable.
- Structured outputs MUST validate against either `ClassificationResult`
  or a Pydantic schema declared by the calling phase. Unvalidated payloads
  are stored in `ollama_model_runs.raw_payload` with
  `status='validation_error'` and never propagate to classifications.
- The orchestrator is the only place that knows model names. No other
  module should reference `glm-4.7-flash` or `qwen3:8b` literally.
  (`GlmOcrNormalizer` is the documented exception — it predates this
  phase and stays as-is.)
- `prompt_version` is bumped any time the prompt text changes. Old runs
  remain valid for evidence; new runs are not idempotent against old
  ones.

**Definition of done:**

- `check-ollama-models-nc` reports green on a fresh dev box.
- `run-llm-doc-probe-nc` produces a validated JSON output for at least
  one real corpus document.
- `ollama_model_runs` rows exist for the probe runs.
- No existing extraction or OCR path has been altered (the orchestrator
  is purely additive at this stage).

### Phase 3 — Multi-dimensional flags ✅ complete (2026-04-30)

**Goal:** capture the boolean flags that downstream consumers care about,
each as its own classifier so they can be reviewed independently.

**Deliverables:**

Add stages to `document_classifications` for each of:

- `flag_is_final`
- `flag_is_proposed`
- `flag_is_redline`
- `flag_is_confidential`
- `flag_has_rate_tables`
- `flag_has_leaf_numbers`
- `flag_is_compliance_filing`

Plus deterministic-extraction stages:

- `utility` (DEC, DEP, Duke Energy Carolinas, Duke Energy Progress, etc.)
- `docket_number` (E-2 Sub ####, E-7 Sub ####)
- `effective_date` (when textually present)
- `tariff_family` (for documents already classified as tariff/rider type)

Each stage has its own classifier (rule-based for v1) emitting
`ClassificationResult`. Some signals already exist in `parse_attempt_logs`
or `historical_documents.metadata_json` — surface them as classifications
rather than duplicating.

**Reuse points:**

- `_apply_legacy_attachment_matching_hints` already detects
  docket/utility/leaf signals. Extract that logic into named classifiers.
- `_classify_page_doc_type` in
  [pipeline/segmentation.py](/c:/Python/Duke/Standalone/src/duke_rates/historical/ncuc/pipeline/segmentation.py)
  already classifies tariff vs procedural per page. Use as input to flag
  classifiers.
- `document_fingerprints` (the existing redline table) already detects
  `is_redline_candidate`. Promote to `flag_is_redline` classifier.
- Rate-table detection already exists in `native_tables.py`. Promote to
  `flag_has_rate_tables`.

**Constraints:**

- Each flag classifier must be independently runnable and testable.
  Don't bundle them into a single "score everything" function.
- Confidence scoring is per-flag — `flag_is_proposed` confidence 0.9 with
  `flag_is_redline` confidence 0.2 is a valid state.

**Definition of done:**

- A document classified as `RIDER` also has rows for `flag_is_proposed`,
  `flag_is_redline`, `flag_has_rate_tables`, `utility`, `docket_number`.
- The disagreement report covers each flag stage independently.
- Existing redline detection logic in extract-rates now reads
  `flag_is_redline` from `document_classifications` instead of
  `document_fingerprints`. (Migration is opt-in: the existing field stays;
  the consumer just prefers the new source when present.)

### Phase 4 — Embedding similarity classifier ✅ complete (2026-04-30)

**Goal:** add embeddings as a second axis for classification, so a new
document can be matched against known reviewed examples by semantic
similarity, not just rules.

**Deliverables:**

1. New `document_embeddings` table:
   `id, source_pdf, file_hash, embedding_kind, embedding_model,
   embedding_version, vector BLOB, metadata_json, created_at`.
   `embedding_kind` covers the proposed slices: `full_text`,
   `first_3_pages`, `title_block`, `rate_table_text`,
   `order_conclusion_section`.
2. Generation via the Phase 2.5 orchestrator using the `embedding_primary`
   and `embedding_secondary` roles (currently `qwen3-embedding:0.6b` and
   `snowflake-arctic-embed2:latest`). Retain the `legacy_embedding_baseline`
   role (`nomic-embed-text:latest`) for regression comparison only — it is
   not the default. Storing two embedding kinds per slice (primary +
   secondary) is the cheapest way to catch model-specific failure modes
   before they bias the classifier; if disk pressure becomes real, drop
   the secondary first.
3. CLI `embed-corpus-nc` — generate embeddings for the corpus. Idempotent
   on `(source_pdf, file_hash, embedding_kind, embedding_model,
   embedding_version)`.
4. New classifier in `document_intelligence/embedding_classifier.py`:
   given a new document, retrieve the top-k similar reviewed documents
   and produce a `ClassificationResult` with the dominant `document_type`
   label and confidence based on neighbor agreement.
5. Wire into the importer alongside the rule classifier so each document
   ends up with both a rule-based and embedding-based row in
   `document_classifications` for the `document_type` stage. Do **not**
   collapse them yet — disagreement is the signal we want to see.

**Constraints:**

- Embedding generation should NOT happen during extract-rates. Run as a
  separate background pass.
- Embeddings are cheap to regenerate but storing them costs disk —
  budget ~1 KB per embedding × N kinds × N documents. For ~5000 documents
  × 3 kinds, that's ~15 MB, fine for SQLite.
- Use cosine similarity, not euclidean. Store vectors as raw float32
  bytes so SQLite stays portable; computation happens in Python.
- The embedding classifier needs at least ~50 reviewed documents per
  category to be useful. If review counts are below that, defer the
  classifier rollout but still generate embeddings (they're useful for
  duplicate detection and clustering on day one).

**Definition of done:**

- `document_embeddings` populated for the corpus.
- Disagreement report has a new dimension: rule-classified type vs
  embedding-classified type.
- Cluster report can group by embedding nearest-neighbor instead of
  cluster_signature, surfacing semantic clusters that the rule-based
  signature missed.

### Phase 5 — LLM adjudication (limited, structured) ✅ complete (2026-04-30)

**Goal:** for cases where rules and embeddings disagree, or confidence
is low, an LLM provides a structured second opinion — never the primary
classification source.

**Deliverables:**

1. New classifier in `document_intelligence/llm_classifier.py`:
   - Input: extracted text (first ~2000 chars), the rule classification
     result, the embedding classification result, the candidate taxonomy.
   - Output: strict JSON conforming to `ClassificationResult` shape, with
     fields `document_type, confidence, evidence`.
   - Constraint: the model is shown the available `document_type` labels
     and instructed to return only those + `UNKNOWN`. Validate the
     returned label against the taxonomy table; on mismatch, store as
     `UNKNOWN` with a metadata note rather than accepting an invented
     label.
2. CLI `adjudicate-classifications-nc` — runs the LLM on rows where:
   - rule and embedding classifiers disagree, OR
   - max(rule_conf, embedding_conf) < 0.6, OR
   - any classifier returned `UNKNOWN`.
3. The LLM result is recorded as a separate row in
   `document_classifications` with classifier `llm_<model_name>_v<n>`. It
   does NOT auto-supersede the rule/embedding rows; superseding happens
   only via Phase 6 human review.
4. Few-shot prompt with 5-10 confirmed examples per category drawn from
   reviewed-and-confirmed rows. Cache the prompt; budget the corpus pass
   to be re-runnable in <10 minutes on a local Ollama install.

**Models to consider** (resolved via Phase 2.5 roles, current local
inventory as of 2026-04-30):

- `balanced_classifier` (`qwen3:8b`, fallback `qwen2.5:7b-instruct`) —
  default for `document_type` adjudication.
- `fast_classifier` (`smallthinker:latest`, fallback `qwen3.5:4b`) —
  cheap pre-pass on documents where confidence is already high.
- `structured_extractor` (`glm-4.7-flash:latest`, fallback
  `command-r:latest`) — preferred when the output is a structured rate-row
  schema rather than a single label. Phase 5 of the **coverage** roadmap
  (rate-row LLM extraction) consumes this role.
- `vision_layout` (`qwen3-vl:4b`) — VLM page-image classification when
  text-only adjudication has plateaued. Defer until that's actually true.
- `heavy_reasoning` (`deepseek-r1:14b`, fallback `gemma4:e4b-it-q4_K_M`)
  — escalation for the hardest residual disagreements only. Slow; do not
  put on the hot path.

Start with `balanced_classifier` text-only; only add `vision_layout` or
`heavy_reasoning` once the cheaper roles have a measured ceiling.

**Constraints:**

- LLM responses MUST conform to the JSON schema — use
  `format=json` in the Ollama call and validate before persisting.
- The fail-fast probe pattern from `GlmOcrNormalizer.is_available()` is
  the precedent for handling Ollama health: probe once at startup, cache
  the result, fail loudly. No silent retries on broken models.
- LLM evidence is recorded but is NOT load-bearing without human
  confirmation. The label produced by an LLM only "counts" once it
  appears in a reviewed row.

**Definition of done:**

- Every disagreement / low-confidence row has an LLM adjudication recorded.
- Disagreement report can show three-way splits: rule said X,
  embedding said Y, LLM said Z.
- A human reviewer can read the LLM's evidence list and either confirm or
  override.

**Implementation results (2026-04-30):**

- `LLMAdjudicator` class in `document_intelligence/llm_classifier.py`:
  - Uses `OllamaOrchestrator.generate_json()` with `balanced_classifier` role
  - `LLMAdjudicationVerdict` Pydantic schema validates JSON output (document_type, confidence, reasoning, key_signals)
  - Prompt includes full taxonomy list + descriptions, rule result, embedding result, and document text
  - Label validation against taxonomy: invented labels become UNKNOWN
  - Never raises — returns UNKNOWN with confidence 0.0 on any failure (no_text, no_taxonomy, model_unavailable, orchestrator_error, JSON parse/validation failure)
  - Evidence captures LLM reasoning, key signals, input results, and orchestrator status
- `adjudicate-classifications-nc` CLI command:
  - Queries `document_classifications` for rule+embedding pairs with disagreements, UNKNOWN labels, or low confidence (<0.5)
  - Excludes documents that already have an `llm_%` classification (idempotent)
  - `--dry-run` previews candidates without calling the LLM
  - `--limit` controls batch size (default 10)
  - `--json` emits machine-readable report
  - Summary shows 3-way agreement (rule vs embedding vs llm)
- Wired into `bulk_extractor._record_llm_document_type()`:
  - Checks idempotency (existing `llm_%` row) before calling the model
  - Only fires when: labels differ, either is UNKNOWN, or max confidence < 0.5
  - Lazy-init pattern: health-probes `balanced_classifier` once, caches the adjudicator
  - Called in `extract_charges_from_document` after `_record_embedding_document_type`
  - Side-effect only, never raises — failures are logged at DEBUG level
- Cross-stage report in `report-classification-disagreements-nc` already supports 3-way comparison — LLM rows (`llm_%`) appear alongside rule and embedding rows
- Known limitation: no few-shot examples yet (Phase 6 review queue will supply confirmed examples; prompt cache is deferred to Phase 5.5 overnight loop)

### Phase 5.5 — Overnight document intelligence loop ✅ complete (2026-04-30)

**Goal:** turn Phases 4 + 5 into a single resumable batch that can run
unattended overnight, persist evidence as it goes, and stop safely if
anything degrades. This is the operational glue, not a new classifier.

**Prerequisites:** Phase 2.5 (orchestrator + `ollama_model_runs`) and
at least one of Phase 4 / Phase 5 wired into a callable pass.

**Deliverables:**

1. New CLI `run-overnight-doc-intelligence-nc`. Flags:

   ```
   --max-documents N            (default: unlimited)
   --max-runtime-minutes N      (hard wall clock cap)
   --max-consecutive-failures N (default: 5)
   --stages embed,llm_adjudicate (comma-separated; default: all enabled)
   --since ISO8601              (only documents added/modified after T)
   --dry-run                    (load, plan, log work units; do nothing else)
   --resume                     (skip subjects already covered for the
                                 current prompt_version + model)
   ```

2. **Resume logic.** A document is considered done for a given stage if
   `ollama_model_runs` already has a row with
   `(subject_kind, subject_id, stage, role, model, prompt_version,
   status='ok')`. The loop computes the work set as the set difference
   and processes it in stable id order so partial runs are deterministic.

3. **Stop conditions** (any one ends the run cleanly, with a summary):
   - `--max-documents` reached
   - `--max-runtime-minutes` reached
   - `--max-consecutive-failures` consecutive failed model calls
   - `check-ollama-models-nc` health probe stops returning ok mid-run
     (re-probed every N documents; default 50)
   - SIGINT / SIGTERM (writes the current row, then exits)

4. **Safety constraints (non-negotiable):**
   - **No destructive overwrites.** The loop only inserts new rows into
     `ollama_model_runs` and `document_classifications`. It never updates
     or deletes existing classifications, and it never sets
     `superseded_by` — superseding remains a Phase 6 (human review) act.
   - **Bounded.** No flag combination can produce an unbounded run; the
     defaults must terminate even with `--max-documents` unlimited
     because the wall-clock cap exists.
   - **Resumable.** Re-running with `--resume` after a crash must not
     re-call the model on already-completed `(subject, stage,
     prompt_version, model)` tuples.
   - **Dry-run is honest.** `--dry-run` enumerates the work set, prints
     a summary table (per-stage counts, estimated runtime from the
     orchestrator's running average), and exits with no DB writes and no
     model calls.

5. **End-of-run report.** Persist a JSON summary to
   `docs/reports/overnight_doc_intelligence/<timestamp>.json` with:
   counts per stage, per-status, per-role; longest runs; failed subjects
   with reasons; and the reason the loop stopped.

**Constraints:**

- This phase ships no new classifier logic. Anything it would
  "classify" must already be implemented as a callable in Phase 4 or 5.
- Stage execution order matters: embedding before LLM adjudication
  (the adjudicator can read the embedding result as evidence). Stages
  run sequentially per document, not in a global queue, so a partial
  document never appears in reporting.
- Concurrency is intentionally low (default `--workers 1`). Local
  Ollama saturates a single GPU; parallelism here means failures, not
  throughput.

**Definition of done:**

- A `--dry-run` over the corpus produces a sane work-set table.
- A bounded real run (e.g. `--max-documents 50`) writes
  `ollama_model_runs` rows and at least one `document_classifications`
  row per processed document, then exits cleanly.
- `--resume` on a re-run does zero model calls if nothing else changed.
- The end-of-run JSON exists and is non-empty.

**Implementation results (2026-04-30):**

- `run-overnight-doc-intelligence-nc` CLI command in `cli.py`:
  - Two stages: `embed` (embeddings via `embedding_primary`) and `llm_adjudicate` (LLM via `balanced_classifier`)
  - Safety controls: `--max-documents`, `--max-runtime-minutes`, `--max-consecutive-failures` (default 5)
  - `--resume` checks `ollama_model_runs` for completed `(subject, stage, role, model, prompt_version, status='ok')` tuples — skips already-done work
  - `--dry-run` enumerates work set with estimated runtime (embed: ~2s/doc, LLM: ~8s/call)
  - `--since` ISO8601 filter for incremental runs on recently added documents
  - Periodic health reprobe every N documents (default 50) — aborts if a needed model goes unavailable
  - SIGINT/SIGTERM handling — finishes current document, writes report, exits cleanly
  - End-of-run JSON report at `docs/reports/overnight_doc_intelligence/<timestamp>.json` with full stats, stop reason, and runtime metrics
- Dry run verified: 879 embed calls + 10 LLM adjudicate calls, est. ~30m total (within overnight budget)
- Live test (--max-documents 5 + --stages llm_adjudicate): 5/5 LLM calls succeeded, all 5 persisted as `llm_qwen3:8b_v1` classification rows
- 3-way comparison confirmed: rule=UNKNOWN (conf 0.0) → embedding predicts → LLM adjudicates with high confidence (0.95-1.0)
- Resume logic verified with `ollama_model_runs` idempotency checks
- LLM field-name robustness: accepts `rationale`/`reasoning`, `verdict`/`label`/`document_type` — salvages labels from non-conforming JSON payloads

### Phase 5.6 — LLM-assisted parse diagnosis and regex improvement loop ✅ complete (2026-05-01)

**Goal:** add an LLM-assisted diagnosis and suggestion layer that analyzes *why*
deterministic regex parsers fail and generates candidate fixes — advisory only,
never auto-applied.

**Core principle: LLMs assist parsing, never replace deterministic parsing.
LLM outputs are advisory unless validated by deterministic tests or human review.**

**Deliverables:**

1. **Parse failure diagnosis** (`parse_diagnosis.py`):
   - `ParseFailureDiagnoser` class — selects weak/empty/low-confidence parse
     attempts from `parse_attempt_logs`, sends structured context to an LLM
     (`parse_failure_triage` role, currently `mistral:7b-instruct`), persists root-cause diagnosis
     with evidence and recommended actions to `llm_parse_diagnostics`.
   - Low-confidence diagnoses escalated to `hard_parse_diagnosis` role
     (qwen2.5-coder:7b after the 2026-05 local benchmark).
   - 2026-05 local benchmarks: `smallthinker:latest` returned valid JSON but
     mostly punted to `unknown` / low-confidence labels. The first
     fixture-backed `benchmark-ollama-roles-nc --task all` run moved
     `parse_failure_triage` to `mistral:7b-instruct` primary because it was the
     only tested model with nonzero parse-diagnosis gold accuracy. Keep
     expanding fixtures; the current sample is still small and mistral is
     biased toward `wrong_profile`.
   - Allowed failure types: `wrong_family`, `wrong_profile`, `ocr_noise`,
     `table_layout`, `missing_effective_date`, `bundled_document`,
     `redline_or_proposed`, `no_rate_table`, `partial_span`,
     `normalization_gap`, `regex_gap`, `unknown`.
   - CLI: `analyze-parse-failures-nc` — `--limit`, `--profile`, `--family`,
     `--since`, `--dry-run`, `--json`.

2. **Regex/normalization suggestion generation** (`regex_suggestions.py`):
   - `RegexSuggestionGenerator` class — generates candidate regex patterns and
     normalization rules for failures classified as `regex_gap`,
     `normalization_gap`, or `ocr_noise`.
   - Uses `regex_suggestion` role (`qwen3:8b`, fallback
     `mistral:7b-instruct`, `phi3.5:latest`, `gemma4:e4b-it-q4_K_M`).
   - Exports human-reviewable JSON artifacts to
     `docs/reports/regex_suggestions/YYYY_MM_DD_<profile>.json`.
   - Suggestions are NEVER auto-applied to parser code.
   - CLI: `suggest-regex-fixes-nc` — `--limit`, `--diagnosis-id`, `--profile`,
     `--failure-type`, `--dry-run`, `--json`.

3. **Deterministic validation harness** (`regex_validation.py`):
   - `RegexValidationHarness` class — tests candidate regexes against known-good
     documents (regression), known-failed documents (improvement), and unrelated
     types (false-positive check).
   - Marks suggestions: `accepted_candidate`, `rejected_false_positive`,
     `rejected_no_gain`, `needs_human_review`.
   - Does NOT modify parser code — regexes tested against extracted text only.
   - CLI: `validate-regex-suggestions-nc` — `--limit`, `--suggestion-id`,
     `--dry-run`, `--json`.

4. **Schema-guided LLM fallback extraction** (`schema_extraction.py`):
   - `SchemaGuidedExtractor` class — for documents where deterministic parsing
     fails but text quality is adequate, uses `structured_rate_extraction` role
     (`gemma4:e4b-it-q4_K_M`, fallback `qwen3:8b`) to extract candidate rate
     rows.
   - VLM pathway via `extract_with_layout()` using `layout_table_extraction`
     role (qwen3-vl:4b) for `table_layout` failures.
   - All extractions stored as CANDIDATES in `llm_candidate_rate_extractions`
     — NEVER merged into production `tariff_charges` without validation.
   - CLI: `run-llm-parse-fallback-nc` — `--limit`,
     `--historical-document-id`, `--profile`, `--family`, `--dry-run`,
     `--json`.

5. **Overnight parse-improvement loop** (`parse_improvement_loop.py`):
   - `ParseImprovementLoop` class — integrates diagnosis, suggestion,
     validation, and extraction into a single resumable batch.
   - Same safety pattern as Phase 5.5: health probes, resume logic, wall-clock
     cap, consecutive-failure abort, dry-run, SIGINT/SIGTERM handling.
   - Sequential task execution: diagnose → suggest → validate → extract.
   - End-of-run JSON report at
     `docs/reports/overnight_parse_improvement/<timestamp>.json`.
   - CLI: `run-overnight-parse-improvement-nc` — `--max-documents`,
     `--max-runtime-minutes`, `--max-consecutive-failures`, `--task-kind`,
     `--profile`, `--family`, `--since`, `--dry-run`, `--resume`, `--limit`.

**New DB tables (migration OL-003):**
- `llm_parse_diagnostics` — root-cause diagnoses with failure_type, evidence_json, model tracking
- `llm_regex_suggestions` — candidate regex/normalization suggestions with test cases and status
- `llm_regex_validation_results` — deterministic validation results with before/after metrics
- `llm_candidate_rate_extractions` — LLM candidate rate rows (NEVER production)

**New Ollama roles:** `parse_failure_triage`, `hard_parse_diagnosis`, `regex_suggestion`,
`structured_rate_extraction`, `layout_table_extraction`, `lightweight_batch_classifier`,
`embeddings_clustering`.

**Implementation results (2026-05-01):**
- All 6 modules created: `parse_diagnosis.py`, `regex_suggestions.py`,
  `regex_validation.py`, `schema_extraction.py`, `parse_improvement_loop.py`
- 5 CLI commands registered and verified
- All 4 DB tables created via additive migration
- Dry-run works instantly for all commands (no Ollama calls)
- Live diagnosis tested end-to-end: select → LLM → validate → persist
- Live extraction tested end-to-end with graceful timeout handling
- Bug fixes during verification: health-probe-before-dryrun ordering, family filter SQL scope, extraction candidate deduplication
- All 19 required Ollama models confirmed pulled and available
- Full verification: `run-overnight-parse-improvement-nc --dry-run --task-kind diagnose,suggest,validate,extract --limit 5` ✓

**Model benchmark update (2026-05-05):**
- `parse_failure_triage` primary changed from `smallthinker:latest` to
  `qwen3:8b` after the first benchmark, then to `gemma4:e4b-it-q4_K_M` after
  the 8-model `benchmark-ollama-roles-nc --task parse_diagnosis` run, then to
  `mistral:7b-instruct` after the first fixture-backed multi-task run.
- Fallback order is now `gemma4:e4b-it-q4_K_M`, `qwen3:8b`, `phi3.5:latest`.
  `smallthinker:latest` was removed from this role because it mostly produced
  `unknown`/low-confidence output. `llama3.1:8b-instruct-q4_K_M` failed schema
  validation for this task; `qwen2.5-coder:7b` showed a 20% schema-error rate.
- `hard_parse_diagnosis` still uses `qwen2.5-coder:7b` first, so
  low-confidence triage diagnoses get a genuinely different second opinion.
  Benchmark this role separately before changing it; the 8-model run measured
  normal parse-diagnosis behavior.
- Existing rows in `llm_parse_diagnostics` are not automatically re-run by
  `--resume`; use `--rediagnose-unknown` for a bounded append-only pass over
  prior `failure_type='unknown'` or `confidence=0.0` diagnostics.
- 2026-05-05 loop evaluation found that model quality was no longer the only
  blocker: candidate selection allowed parse attempts with no resolved
  historical document or usable text. `ParseFailureDiagnoser` and
  `SchemaGuidedExtractor` now filter for resolved historical documents with
  page-artifact text or `raw_text_path`, and both text readers fall back to
  `historical_documents.raw_text_path` when page artifacts are empty. A bounded
  live diagnosis after the fix produced actionable `regex_gap` and
  `no_rate_table` diagnoses instead of all-`unknown`.
- 2026-05-05 later update: `analyze-parse-failures-nc` and
  `run-overnight-parse-improvement-nc` now support `--rediagnose-unknown`.
  A 2-row live smoke test repaired one prior unknown to
  `redline_or_proposed` at 0.95 confidence and left one row unknown.
  `structured_rate_extraction` was first moved from large glm/command-r models
  to `qwen3:8b`; the fixture-backed benchmark then moved it to
  `gemma4:e4b-it-q4_K_M` primary because gemma was the only viable tested
  extraction model, with 100% gold accuracy on valid fixture returns and no
  timeouts.
- 2026-05-05 model-benchmark follow-up: `benchmark-ollama-roles-nc` replaces
  the scratch `tmp_model_benchmark.py` workflow with a non-mutating benchmark
  surface for `parse_diagnosis`, `hard_parse_diagnosis`, `regex_suggestion`,
  `structured_rate_extraction`, and `document_classification`. It uses
  production-style prompts/Pydantic schemas, supports explicit model lists,
  runtime/request bounds, and writes timestamped JSON reports to
  `docs/reports/ollama_model_benchmarks/`.
- 2026-05-05 specialization benchmark update: `benchmark-ollama-roles-nc` now
  accepts `--task all` and comma-separated task lists. Multi-task reports add
  per-task rankings, label-bias scores, diversity counts, and model-pair
  disagreement rates so future layered model routing can be based on
  task-specific evidence rather than one parse-diagnosis leaderboard.
- 2026-05-05 regex-suggestion rerun: after schema normalization and expanding
  regex fixtures from 1 to 2 cases, `qwen3:8b`, `mistral:7b-instruct`, and
  `phi3.5:latest` all returned 100% valid JSON with 100% gold accuracy.
  `regex_suggestion` is now `qwen3:8b` primary because it was the fastest among
  those fully valid models. `gemma4:e4b-it-q4_K_M`, `nemotron-3-nano:4b`, and
  `ministral-3:8b` each failed schema validation on one of two cases. The regex
  prompt now explicitly requests `confidence` so valid suggestions should no
  longer report as 0.0 by default.
- 2026-05-05 fixture expansion: added `diagnosis:69` as a second regex gold
  fixture for Residential New Construction incentive/kWh extraction. The
  benchmark can now include explicit regex fixture diagnosis IDs even if a prior
  suggestion row exists, and regex missed-text excerpting prioritizes
  rate/unit/incentive lines in long documents.
- 2026-05-05 gold-fixture update: the benchmark accepts `--fixtures` with
  human-reviewed expected outputs keyed by `(task, case_id)`. Fixture-backed
  runs add `accuracy_pct`; non-fixture runs should be treated as structure,
  speed, bias, and complementarity benchmarks, not true accuracy benchmarks.
  The initial `docs/reports/ollama_model_benchmarks/gold_fixtures.json` set
  contains 20 frontier-reviewed cases across parse diagnosis, regex suggestion,
  structured extraction, and document classification.
- 2026-05-05 regex-suggestion schema update: the fixture-backed run showed all
  tested models emitted usable-looking regex suggestions but failed the strict
  schema because they used `risk_level` and string test cases. `RegexSuggestion`
  now normalizes those variants into `risk` and `{text, should_match}` test-case
  objects. Re-run the regex benchmark before changing parser-suggestion
  workflows.
- Note: `OllamaOrchestrator` fallback is failure handling, not consensus. A
  valid but biased primary response will not trigger fallback. Add a future
  ensemble/validator mode if model disagreement itself should drive escalation.

**Pending for full DOD (require overnight model execution):**
- 25+ diagnoses via `analyze-parse-failures-nc --limit 25`
- 5+ regex suggestions via `suggest-regex-fixes-nc --limit 5`
- Validation harness live test
- Full overnight loop: `run-overnight-parse-improvement-nc --task-kind diagnose,suggest,validate,extract --limit 25 --max-runtime-minutes 480`

### Phase 6.5 — Database Intelligence and Corpus Analytics ✅ complete (2026-05-01)

**Goal:** use deterministic SQL + LLM summarization to understand corpus-level
behavior — gaps, anomalies, trends — without depending on future ML training.

**Deliverables:**

1. `document_intelligence/database_reports.py` — 7 deterministic SQL report
   functions: `find_missing_versions`, `find_unknown_documents`,
   `find_low_quality_parses`, `find_stale_artifacts`,
   `find_duplicate_documents`, `find_family_lineage_gaps`,
   `find_docket_coverage_summary`. Each returns structured Python dicts.
   Master function `build_database_intelligence_report` runs all 7.

2. `document_intelligence/db_llm_analysis.py` — LLM summarization models
   (`IntelligenceSummaryResponse`, `KeyFinding`, `HighValueAction`, etc.),
   SQL safety validator (`validate_sql_safety`, `enforce_limit`,
   `execute_safe_query`), SQL generation from natural language via
   `code_model` role, and result summarization via `balanced_classifier`.

3. CLI `report-database-intelligence-nc` — runs all 7 deterministic reports,
   produces structured JSON saved to
   `docs/reports/database_intelligence/YYYY_MM_DD.json`. Supports
   `--limit`, `--family`, `--docket`, `--since`, `--dry-run`, `--json`.

4. CLI `summarize-database-intelligence-nc` — feeds compact report into
   `balanced_classifier` LLM for executive summary, key findings, root
   causes, and high-value action recommendations. Summary persisted
   separately as `YYYY_MM_DD_summary.json`.

5. CLI `ask-ncuc-db` — natural-language database querying with strict safety:
   SELECT-only, table whitelist, LIMIT enforcement (max 100 rows), query
   timeout, multi-statement rejection. SQL shown before execution. Every
   query logged to `database_intelligence_runs`.

6. CLI `run-overnight-db-intelligence-nc` — unattended overnight loop:
   deterministic reports → LLM summarization → anomaly identification →
   morning report. Supports `--max-runtime`, `--dry-run`, `--resume`.

7. `database_intelligence_runs` table (migration DB_INTEL-001) — run
   logging with run_type, status, question, generated_sql, safety_check,
   execution_status, duration_ms, and config_json.

8. MCP design document at `docs/mcp/ncuc_database_intelligence.md`
   (planning only) — defines 5 tools: `ncuc_db_intelligence_report`,
   `ncuc_db_ask`, `ncuc_db_summarize`, `ncuc_db_schema_explore`,
   `ncuc_db_run_history`.

**Constraints:**

- Read-only database access — no INSERT/UPDATE/DELETE in report queries.
- LLM output is advisory, never auto-modifies code or data.
- Deterministic reports are always the source of truth; LLM summaries are
  separate files.
- SQL assistant enforces SELECT-only, LIMIT cap, table whitelist, timeout.
- No modification of existing parsing/classification/extraction logic.
- All reports additive — follow the existing `connect()` +
  `try/finally conn.close()` pattern.

**Definition of done:**

- All 7 deterministic reports run without error on the live corpus.
- `report-database-intelligence-nc` saves a valid JSON report.
- `summarize-database-intelligence-nc --dry-run` produces compact JSON.
- `ask-ncuc-db --dry-run` generates and displays SQL safely.
- SQL safety validator correctly blocks DROP, DELETE, INSERT, UPDATE,
  multi-statement, and non-SELECT queries.
- `run-overnight-db-intelligence-nc --dry-run` enumerates stages.
- `database_intelligence_runs` table created and idempotent.
- MCP design document complete.
- Roadmap updated.

### Phase 6 — Review queue + training dataset export

**Goal:** turn classifications into reviewed labels suitable for training
a future ML classifier.

**Deliverables:**

1. New `classification_reviews` table:
   `id, classification_id (FK), reviewer, decision, corrected_label,
   notes, reviewed_at`. `decision` ∈ `{confirm, correct, reject, defer}`.
   When a review confirms or corrects a classification, the original row
   gets `superseded_by` pointing at a new row recorded by the human
   reviewer (classifier=`human_reviewer`, classifier_version=`<id>_v1`).
2. CLI `review-queue-nc`:
   - List rows needing review (low-confidence, three-way-disagreement,
     `UNKNOWN`, high-value categories).
   - For each, show the document path, extracted excerpt, all classifier
     outputs and evidence, and prompt for a decision.
   - Persist the review.
3. CLI `export-training-dataset-nc`:
   - Output JSONL or Parquet with one row per reviewed document.
   - Schema includes: `source_pdf, file_hash, fingerprints,
     embeddings_metadata, deterministic_features, classifier_outputs (one
     per stage), reviewed_label, evidence_spans, provenance`.
   - Filter to only documents with at least one reviewed row.

**Constraints:**

- Reviews are append-only. A correction to an existing review creates a
  new row, never overwrites.
- The export is read-only; it produces a snapshot for downstream training.
- Don't build a web UI yet. The CLI is sufficient for the volume of
  review work we're talking about (~hundreds of documents, not millions).

**Definition of done:**

- A reviewer can run `review-queue-nc`, decide on 50 documents, and the
  decisions persist.
- `export-training-dataset-nc` produces a valid JSONL with reviewed labels.
- The export schema is sufficient input for a future training pass —
  document type classifier, redline detector, etc. — without further
  schema changes.

### Phase 7 (later, optional) — Trained classifier replaces or augments rules

Only attempted after Phase 6 has accumulated meaningful reviewed data
(estimate: at least ~200 reviewed documents per category we want the
classifier to recognize — fewer for binary flags). Approaches:

- Fine-tune a small instruction-tuned model (Llama 3.2-1B, Qwen 2.5-1.5B)
  on the reviewed JSONL.
- Or: train a classical classifier (XGBoost, logistic regression) on
  fingerprint + embedding features. Faster, more interpretable, often
  competitive on small datasets.

The trained classifier becomes another row in `document_classifications`
with classifier=`trained_v1`. It does not replace rules — both run in
parallel, disagreement remains observable.

## Branching beyond NCUC

The polymorphic `subject_kind` field in `document_classifications` and
the free-string `stage` field were chosen specifically so this
infrastructure can extend beyond NCUC dockets without schema changes.
When the time comes to handle (e.g.) FERC filings, ISO documents, or
RFP responses:

- Add new `document_type` rows for the new corpus.
- Add new `subject_kind` values if the new corpus has different subject
  shapes (e.g. `subject_kind = 'ferc_filing'`).
- Add new classifier modules for any corpus-specific rules.
- The classification result type, persistence, fingerprinting, embeddings,
  and review queue stay unchanged.

This is why we did NOT seed a hardcoded NCUC-only schema in Phase 1.

## Operating principles for whoever picks this up

1. **Read this document end-to-end before starting.** The temptation to
   skip to "implement Phase X" without understanding what came before
   has resulted in rebuilds and false starts in past sessions.
2. **Pick exactly one phase per session.** Do not bundle phase 2 and 3
   together because they "feel related." Each phase is sized to be
   reviewable; bundling defeats the point.
3. **Validate on real data before declaring a phase done.** Definition-of-
   done sections are not optional. A phase that ships code without
   running it on the corpus is not done.
4. **Update this roadmap when a phase lands.** Mark it ✅, update the
   "what already exists" table, note any deviations from the plan and why.
5. **Run `gitnexus_impact` before edits to shared code, per
   [CLAUDE.md](/c:/Python/Duke/Standalone/CLAUDE.md).** Most of the
   classification surfaces are reachable from many entry points. The
   change has higher blast radius than it looks.
6. **Don't add a new table when an existing column would do.** And don't
   reuse an existing table when its semantics don't match — see the
   `document_fingerprints` vs `document_fingerprints_v2` decision in
   Phase 1 for the precedent.

## Appendix: schemas and types

### `ClassificationResult`

See [src/duke_rates/classification/result.py](/c:/Python/Duke/Standalone/src/duke_rates/classification/result.py).

```python
class ClassificationResult(BaseModel):
    label: str
    confidence: float                          # 0.0..1.0
    classifier: str
    classifier_version: str = ""
    evidence: list[dict[str, Any]] = []        # [{"kind": "...", "value": "...", "weight": 1.0}, ...]
    alternatives: list[tuple[str, float]] = [] # [("runner_up_label", raw_score), ...]
    metadata: dict[str, Any] = {}
```

### `document_classifications` table

See [src/duke_rates/db/schema.py](/c:/Python/Duke/Standalone/src/duke_rates/db/schema.py).

Polymorphic on `(subject_kind, subject_id)`. Idempotent on the UNIQUE key
`(subject_kind, subject_id, stage, classifier, classifier_version)`. Use
`superseded_by` to overlay second-opinion or human-reviewed labels
without deleting the original.

### `document_fingerprints_v2` table

See [src/duke_rates/db/schema.py](/c:/Python/Duke/Standalone/src/duke_rates/db/schema.py).
Independent of any classifier. Populated for every PDF the pipeline
encounters, regardless of whether we know how to classify it yet.

### Phase status table

| Phase | Status | Notes |
|---|---|---|
| 1. Classification observability | ✅ complete (2026-04-30) | Fingerprints: 8,812 / 4,406 PDFs / 40 clusters. Disagreement report wired (0 findings — only one classifier writing). 390/879 family_mapping coverage; 489-doc gap accepted as a Phase 2 side-effect close, not a dedicated backfill. All existing rows are `backfill_v1` at confidence 1.0 — disagreement signal will come from Phase 2's new classifier, not retro-scoring. |
| 2. Document type taxonomy | ✅ complete (2026-04-30) | 12 types seeded across 6 categories. 879/879 historical_documents classified. Distribution: ORDER_FINAL 50%, TARIFF_SHEET 31%, COVER_LETTER 10%, UNKNOWN 5%, TESTIMONY 4%. Confidence spans 0.0–0.7. ORDER_FINAL over-count is a pre-existing legacy-classifier issue, deferred to Phase 3 flag classifiers. |
| 2.5. Ollama model orchestration layer | ✅ complete (2026-04-30) | All 10 roles green on `check-ollama-models-nc`. `run-llm-doc-probe-nc` smoke test passed (validation_error on missing classifier field — model returned valid JSON, orchestrator correctly validated). `ollama_model_runs` persistence confirmed. Per-role `probe_kind` (generate/embed/tags_only) and `timeout_s` overrides added to handle embedding models, vision models, and slow-loading MoE models. |
| 3. Multi-dimensional flags | ✅ complete (2026-04-30) | 11 flag classifiers in `flag_classifiers.py`. 879/879 docs backfilled (10,938 total rows). Stages: `flag_is_final` (37% true), `flag_is_proposed` (6% true), `flag_is_redline` (2.5% true), `flag_is_confidential` (2% true), `flag_has_rate_tables` (34% true), `flag_has_leaf_numbers` (93% true), `flag_is_compliance_filing` (16% true), `utility` (DEP 48%/DEC 44%), `docket_number` (2% matched), `effective_date` (89% found), `tariff_family` (100% via metadata). Redline skip in extract-rates now prefers `document_classifications.flag_is_redline` over `document_fingerprints.is_redline_candidate`. Disagreement report supports flag stages via `--stage`. `backfill-flag-classifications-nc` CLI added for reprocessing. |
| 4. Embedding classifier | ✅ complete (2026-04-30) | `document_embeddings` table (OL-002), `text_slicer.py` (5 slice kinds), `embed-corpus-nc` CLI (idempotent, dual-model), `EmbeddingKNNClassifier` (cosine similarity, weighted voting, self-match exclusion), wired into `bulk_extractor._record_embedding_document_type()` as second document_type row with `classifier="embedding_knn_v1"`. Cross-stage report (`--cross-stage document_type`) shows rule vs embedding comparison with agreement/disagreement/overrule_candidate status. 100-doc validation: 13/20 agreements, 7 overrule candidates (rule=UNKNOWN, embedding=high-confidence). `backfill-embedding-classifications-nc` CLI added for reprocessing. Both 1024-dim models working via Ollama. |
| 5. LLM adjudication | ✅ complete (2026-04-30) | `LLMAdjudicator` class, `adjudicate-classifications-nc` CLI, 3-way disagreement support in cross-stage report, wired into `bulk_extractor._record_llm_document_type()`. Known limitation: no few-shot examples yet (deferred to Phase 6 review queue). |
| 5.5. Overnight document intelligence loop | ✅ complete (2026-04-30) | `run-overnight-doc-intelligence-nc` CLI with resume, dry-run, wall-clock cap, consecutive failure abort, SIGINT/SIGTERM handling, end-of-run JSON reports. Verified: 879-doc dry run, 5-doc live test with LLM adjudication. |
| 5.6. LLM-assisted parse diagnosis | ✅ complete (2026-05-01) | Four new modules: `parse_diagnosis.py` (failure classification), `regex_suggestions.py` (regex/normalization candidate generation), `regex_validation.py` (deterministic validation harness), `schema_extraction.py` (schema-guided LLM fallback extraction). Five new CLIs: `analyze-parse-failures-nc`, `suggest-regex-fixes-nc`, `validate-regex-suggestions-nc`, `run-llm-parse-fallback-nc`, `run-overnight-parse-improvement-nc`. Four new DB tables (OL-003 migration). Seven new Ollama roles. All LLM outputs are advisory — no parser code is auto-modified. |
| 6.5. Database Intelligence | ✅ complete (2026-05-01) | `database_reports.py` (7 deterministic SQL reports), `db_llm_analysis.py` (LLM summarization + SQL safety validator), 4 new CLIs (`report-database-intelligence-nc`, `summarize-database-intelligence-nc`, `ask-ncuc-db`, `run-overnight-db-intelligence-nc`), `database_intelligence_runs` table (DB_INTEL-001), MCP design doc. All read-only. |
| 6. Review queue + training export | ⏳ not started | |
| 7. Trained classifier | ⏳ not started | Requires Phase 6 data. |
