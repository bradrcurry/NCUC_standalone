# Roadmap

This roadmap is intentionally updated to reflect the current state of the repo, not the original greenfield plan.

## Implemented and Working

### North Carolina residential history

- DEP `RES` base-rate history exported and queryable from SQLite
- DEP clean rider-summary history from normalized `Leaf 600` data
- DEP provisional pre-clean-summary rider history persisted in DB tables
- DEC `RS` base and rider history exported and queryable
- Canonical residential DEP-vs-DEC timeline export
- Coverage-status handling:
  - `same_day`
  - `carried_forward`
  - `uncovered`

### Charts and app layer

- reusable Plotly chart layer
- DEP-specific Streamlit app
- DEP vs DEC residential comparison app
- app now defaults to cached canonical CSV for performance

### Billing and validation

- bill PDF parsing for actual Duke bills
- XML interval usage ingestion
- bill component observation layer
- Progress NC bill reconciliation
- fixed-charge proration for short partial bills
- mid-bill base-rate split handling
- storm rider stacking and effective-date proration
- DEP `R-TOU-CPP` parsing and usable reconciliation path
- saved-bill validation currently at:
  - `12/12 good_match`
  - max absolute delta `$0.45`

### Audits and validation outputs

- DEP validation export
- DEC validation export
- rider date audit
- bill rider effective-date audit
- bill validation rollup
- DB-driven NC coverage, anomaly, and schedule-inventory exports
- initial NC document-intelligence audit export for malformed/zero-charge historical rows
- DEP-focused rider-gap, compliance-bundle, storm-rider, and storm-history exports

### Document intelligence foundations

- initial `src/duke_rates/document_intelligence/` package added
- normalized document representation
- heuristic fingerprinting / classification
- schema-mapped extraction sidecar
- source-aware validation
- ML-ready training-record capture
- first live integration in historical bulk extraction

## Current Practical Baseline

If the goal is analysis, dashboarding, or handoff, the current baseline should be treated as:

- NC residential is real and operational
- historical comparison exports are usable
- billing validation is strong for the validated bill set
- provenance is explicit and surfaced in outputs
- the DB audit layer is now strong enough to drive backfill and reparse work from generated reports instead of hand-maintained issue lists
- authenticated NCUC portal harvesting is working and now feeds directly into the historical intake workflow
- the limiting factor has shifted from document discovery to intake quality and canonicalization of imported historical material

The repo is no longer just “crawl and parse some PDFs.”

## Next Priorities

### 0. Structural correctness fixes (highest leverage — do before expanding scope)

These items were identified in a technical code review (2026-03-21). Full details with file
references and acceptance criteria are in [docs/technical_debt.md](technical_debt.md).

**0a. ~~Add `utility` column to `ncuc_ingest_segments` and `rider_summary_blocks`~~ — DONE 2026-03-21**

`utility TEXT` column added via migration in `schema.py:migrate()`. `calculate_bill()`,
`load_ingest_results()`, and `load_rider_summaries()` all accept and use the `utility` parameter.
All existing rows backfilled (DEP/DEC from docket_number then docket_dir patterns).

**0b. ~~Add `UNIQUE` constraint to `rider_summary_blocks`~~ — DONE 2026-03-21**

`idx_rider_blocks_unique` unique index added via migration. Duplicate cleanup runs automatically
before index creation. DB now rejects duplicate `(docket_dir, source_pdf, rate_class, effective_date)` inserts.

**0c. ~~Consolidate duplicate season-matching tables~~ — DONE 2026-03-21**

`src/duke_rates/billing/season_utils.py` created with unified `SEASON_MONTHS`, `_normalize_season_label()`,
and `season_matches()`. Both `engine.py` and `ncuc_loader.py` now import and call `season_matches()`.
35 tests in `tests/test_season_consistency.py`. See TD-003 in technical_debt.md.

**0d. ~~Fix `_NOW` module-level timestamp in `ncuc_loader.py`~~ — DONE 2026-03-21**

Module-level `_NOW` constant removed. All three call sites now use inline `datetime.now(UTC).isoformat()`.

### 0e. Workflow and tooling institutionalization — IN PROGRESS 2026-03-31

Goal: reduce repeated agent token usage by moving recurring reasoning steps into
local commands, compact DB-backed summaries, and sanctioned operator workflows.

Current direction:
- keep [AGENT_ONBOARDING.md](/c:/Python/Duke/Standalone/AGENT_ONBOARDING.md)
  as a compact router rather than a large knowledge dump
- use [operator_workflows.md](/c:/Python/Duke/Standalone/docs/operator_workflows.md)
  as the canonical sanctioned workflow catalog
- require workflow changes to update canonical docs in the same task
- promote repeated script- or SQL-based investigations into stable CLI commands

Highest-leverage tooling targets:

Current operational focus has shifted to:
- reducing provisional and malformed NC family sprawl after the latest harvest/import wave
- canonicalizing or retiring misclassified historical `doc-*` / `program-*` rows
- using the new document-intelligence audit to drive DEC/NC canonicalization instead of hand-maintained candidate lists
- targeting the anomaly audit's highest-value remaining DEP / DEC schedule gaps instead of broad rider cleanup
- keeping handoff docs synchronized with DB-generated reports so future sessions do not drift

Success condition:
- a new agent can select and run a workflow with minimal repo scanning
- current system state is visible through summary commands rather than manual SQL
- improvements to process/tooling are documented and inherited automatically

### 1. Cleanup and canonicalization — DONE 2026-03-21

- ~~undated DEP component rows~~ — already cleaned (0 NULL rider_effective_date rows remain)
- ~~canonical rider-component function with `source_kind` discriminator~~ — DONE
  - `src/duke_rates/analytics/canonical_rider_components.py`
  - `load_dep_res_canonical_rider_components()` — DEP, unifies `clean_leaf600` (2023+) + `provisional_ingest` (2016–2022)
  - `load_dec_rs_canonical_rider_components()` — DEC, `clean_leaf600` only
  - 17 tests in `tests/test_canonical_rider_components.py`
- duplicate raw summary-source rows: `rider_summary_blocks` UNIQUE index now enforces deduplication at DB level (TD-002); remaining multi-source rows for same effective date are intentional (multiple PDFs per filing)

### 2. Trust / validation layer — DONE 2026-03-21

- ~~build rider-family trust scores~~ — DONE
  - `src/duke_rates/analytics/rider_trust.py`
  - `load_rider_trust_table()` — 172 rows (DEP + DEC), four scoring dimensions (source_quality, date_completeness, bill_support, continuity)
  - `trust_summary()` — compact QA handoff dict
  - `export_rider_trust_table()` — CSV export
  - Current results: 147 high, 23 medium, 2 low (SCR/STS provisional-only)
  - 15 tests in `tests/test_rider_trust.py`

### 2a. Pipeline Refactoring — DONE 2026-03-25

- ~~Refactored NCUC historical recovery pipeline into a staged, page-aware, evidence-driven pipeline~~
- Added `triage.py`, `page_miner.py`, `segmentation.py`, `family_matcher.py`, and `metadata_extractor.py`.
- Solved false-positive matching by explicitly bounding multi-leaf compliance books into isolated `TariffSpan` objects.

### 2b. Historical parser and OCR routing hardening — IN PROGRESS

- Added centralized Duke company alias normalization and importer-side company inference.
- Added parser-profile scaffolding for historical extraction so new rules can be isolated by company, family, era, and document structure.
- Added triage-level OCR routing signals:
  - `ocr_confidence_score`
  - `structure_complexity_score`
  - `gpu_ocr_candidate`
  - `triage_flags`
- Added targeted reprocessing foundations:
  - `historical_processing_runs` for versioned extraction attempts
  - `historical_reprocess_queue` for prioritized selective reparsing
  - CLI support to enqueue, inspect, and process weak historical documents
- Added OCR queue/cache foundations:
  - `ocr_processing_queue` for OCR_REQUIRED discovery records
  - `ocr_artifacts` for persistent OCR sidecars and metadata
  - OCR sidecar reuse in the CPU OCR path to avoid repeated expensive reruns
  - CLI support to enqueue, inspect, and process OCR work selectively
- Planned selective Docling integration:
  - use `docling` as a heavy-analysis backend for OCR-heavy, table-heavy,
    layout-heavy, or repeatedly weak documents
  - keep the native-text path and current OCR path as the default fast path
  - cache Docling JSON / text / table artifacts by file hash and backend version
  - feed Docling output back into the same page/span/parser flow instead of
    creating a disconnected conversion pipeline
- Added page/span artifact retention foundations:
  - `ncuc_page_artifacts` for mined page evidence reuse
  - `ncuc_span_artifacts` for bounded span reuse
  - importer-side reuse of cached pages/spans by source hash
- Added stage-aware invalidation/versioning foundations:
  - shared current stage version policy for OCR/page/span/parser stages
  - OCR sidecars invalidate automatically when backend version changes
  - stale historical documents can be listed/queued by stage mismatch reason
  - stale reprocess runs now bootstrap missing `tariff_versions` instead of
    failing hard on otherwise-runnable historical documents
  - stale reprocess runs now refresh page/span artifacts before extraction when
    the queued reason is a stale or missing artifact stage
  - the live stale-stage backlog has been driven to `0` under the current
    stage versions
- Added dependency-aware invalidation foundations:
  - parser-profile dependency rules for known historical profiles
  - impact preview/queue commands for profile-specific reparsing
  - selective reparsing can now target affected families/documents instead of only stage-level stale checks
  - latest parse-attempt selector reasons/signals now feed profile-impact targeting,
    reducing reliance on exact family lists alone
- Added conservative parser fallback sequencing foundations:
  - historical parses now persist fallback recommendation order
  - alternate supported profiles are auto-tried when the initial extraction is empty
    or when a weak parse has a materially better fallback result
  - fallback decisions are persisted in parse-attempt, fingerprint, review, and processing-run metadata
  - fallback scoring now considers charge-type coverage and extraction completeness,
    not just raw charge-count gain
- Hardened bulk extraction operator behavior:
  - `extract-rates-nc` now targets only historical documents with linked
    `tariff_versions`
 - Improved bundle-style remine behavior:
   - book-style DEP tariff PDFs now split on schedule-title transitions even
     without clean leaf headers
   - generic provisional families such as `TYPEOFSERVICE` and
     `EFFECTIVEFORSERVICE` are no longer treated as normal family-match targets
   - legacy raw-attachment hints are now selected per span instead of only when
     a whole regulator PDF points to one family
   - live effect: `E-2, Sub 1142` now remines into bounded schedule spans and
     recreates `nc-progress-doc-RESIDENTIALTIMEOFUSEENERGY`
 - Improved weak-unbounded legacy queue accuracy:
   - leftover legacy raw rows now infer `discovery_record_id` from nested
     regulator `local_file` metadata
   - rows are now split more honestly between:
     - `retire_legacy_raw_attachment` for records like `957` where cached page
       evidence shows the regulator PDF is procedural-only and the old raw rows
       are false-positive residue
     - `retire_bundle_reference_residue` for bundle leftovers like `1124` once
       cached span evidence proves the old raw rider rows only appear as rider
       application references inside already-bounded host spans
     - `manual_lineage_review` for the smaller remainder that still needs true
       lineage investigation
   - the redundant Progress legacy raw queue has been driven back to `0`
 - added `python -m duke_rates lineage list-bundle-reference-legacy-raw-historical-nc`
     to surface the host bounded documents behind those bundle-reference rows
  - the remaining Progress weak-unbounded legacy backlog from `1124`
     (`602`, `605`, `610`, `718`) has now been retired from the live DB
   - the `957` false-positive residue (`leaf-613`, `leaf-672`) has been
     removed from the live DB
   - the stale-stage queue has been driven back to `0` after a full current
     reprocess pass under the latest page/span versions
   - the Carolinas weak-unbounded current-PDF queue is no longer a broad
     backlog:
     - `carolinas_current_leaf_bridge` now covers `nc-carolinas-schedule-HLF`
     - `carolinas_solar_choice_rider` now covers
       `nc-carolinas-rider-NMB` and `nc-carolinas-rider-NSC`
   - the only remaining current-PDF Carolinas weak-unbounded row is
     `nc-carolinas-schedule-PP`
   - that final Carolinas row has now been repaired through
     `python -m duke_rates lineage repair-historical-current-snapshot`, so both the
     Progress and Carolinas weak-unbounded queues are currently `0`
   - added `python -m duke_rates lineage list-placeholder-heading-historical-nc`
     plus `python -m duke_rates lineage retire-historical-document` to remove bounded
     heading residue such as `TYPE OF SERVICE` / `Effective for service`
   - used live to retire `19` Carolinas placeholder heading rows that were
     inflating the generic review backlog without representing real tariff
     families
   - `parse-review-queue` and `parse-review-summary` now count only the latest
     parse attempt per source/page/stage instead of every historical rerun
   - added `python -m duke_rates reconcile-skipped-parse-reviews` to repair
     stale skipped rows left behind from older queue behavior
   - targeted requeue filters now apply before the queue limit is enforced, so
     family-specific cleanup passes like `--family-key nc-progress-leaf-672`
     no longer miss the target family just because unrelated weak rows ranked
     ahead of it
   - live `parse-review-summary --json` is now down to `68`
     `outstanding_needs_review`
   - added dedicated historical profiles:
     - `progress_energywise_business` for `nc-progress-leaf-706`
     - `progress_powerpair_pilot` for `nc-progress-leaf-770`
     - `progress_demand_response_automation` for `nc-progress-leaf-717`
     - `progress_sunsense_solar_rebate` for `nc-progress-leaf-716`
     - `progress_meter_related_optional_programs` for `nc-progress-leaf-661`
     - `progress_standby_service` for `nc-progress-leaf-653`
     - `progress_greenpower_program` for `nc-progress-leaf-642`
     - `carolinas_lighting_schedule` for Carolinas `OL` / `PL` / `FL` /
       `YL` / `GL` lighting books
     - `carolinas_small_customer_generator` for `nc-carolinas-rider-SCG`
     - `carolinas_energy_efficiency_rider` for `nc-carolinas-rider-EE`
     - `carolinas_economic_development_rider` for `nc-carolinas-rider-EC`
     - `carolinas_interruptible_service_rider` for `nc-carolinas-rider-IS`
     - `green_source_advantage_rider` for `nc-carolinas-rider-GSA` and
       `nc-progress-leaf-665`
     - `carolinas_schedule_bridge` for historical Carolinas
       `SCHEDULE I`, `SCHEDULE OPT-E`, `SCHEDULE TS`, and `SCHEDULE WC`
   - the shared Carolinas leaf parser now recognizes `Basic Facilities Charge`
     rows directly, improving fixed-charge extraction across those schedule
     books without adding one-off parsers for every family
   - added formula-only skip handling for `nc-progress-leaf-660`
     (`skipped_formula`) so customer-specific Premier Power Service riders no
     longer inflate `generic_residential` weak parses
   - widened formula-only skip handling for `nc-progress-leaf-672` so real
     `Rider CEI` sheets now leave the weak backlog as `skipped_formula`
   - widened formula-only skip handling for:
     - `nc-progress-leaf-712`
     - `nc-progress-leaf-721`
     - `nc-progress-leaf-723`
     - `nc-progress-leaf-640`
     - `nc-progress-leaf-663`
   - tightened SCG reference-only handling so continuation/rules pages and
     incidental `Rider SCG` mentions inside `Schedule RT` no longer inflate the
     backlog as empty `generic_residential` parses
   - `parse-review-queue` / `parse-review-summary` are now lineage-aware and
     operationally deduplicated:
     - deleted historical docs no longer appear in the queue
     - stale family/company metadata is replaced by current historical lineage
     - repeated reruns of the same historical document now collapse to the
       latest operational attempt per parser stage
  - operator output now reports how many otherwise-extractable historical
    documents are still blocked by missing version links
- Added conservative reference-document skipping for historical extraction:
  - obvious program/service-regulation/reference families can now exit as
    `skipped_reference` instead of inflating `generic_residential` weak-parse
    backlog
  - rule-based review outcomes now treat `skipped_*` historical parses as
    accepted rather than `needs_review`
  - this is intentionally narrow and currently covers clear non-billable cases
    such as `leaf-801` and `leaf-802`, while leaving mixed service-regulation
    sheets like `leaf-800` in review until they have a better dedicated path
- Added a dedicated historical `progress_residential_flat` parser profile:
  - modern Progress flat residential sheets like `nc-progress-leaf-500` no
    longer have to fall straight to `generic_residential`
  - live limited extraction now shows at least the strongest modern `leaf-500`
    case selecting `progress_residential_flat` and producing a fuller charge set
  - the new profile is now integrated with parser-profile impact targeting for
    selective reparsing
- Added a dedicated historical `progress_billing_adjustments` parser profile:
  - `nc-progress-leaf-601` Rider BA billing-adjustment tables now use the
    existing Progress-specific parser logic instead of the generic fallback
  - the profile is integrated with parser-profile impact targeting so `leaf-601`
    documents can be selectively reprocessed after BA-parser changes
  - live reparsing now shows true `leaf-601` sheets producing structured
    adjustment charges under `progress_billing_adjustments` instead of empty
    `generic_residential` parses
- Added a dedicated historical `progress_single_value_rider` parser profile:
  - one-value Progress rider leaves such as `nc-progress-leaf-608`,
    `nc-progress-leaf-609`, and `nc-progress-leaf-610` now parse as bounded
    tariff sheets instead of being skipped as order-like pages
  - parser-profile impact targeting now covers those leaves for selective
    reparsing after rider-parser changes
  - live reruns now produce single adjustment charges under
    `progress_single_value_rider`
  - the extraction scorer now treats those high-confidence one-charge results
    as accepted strong parses instead of misclassifying them as weak due to an
    intentionally sparse charge set
- DEP rider/storm audit layer is now materially cleaner:
  - `dep_residential_rider_action_queue` is currently empty
  - `dep_residential_rider_repair_plan` is currently empty
  - `dep_compliance_bundle_audit` currently shows the six audited DEP residential rider families healthy
  - `dep_storm_rider_audit` currently shows `leaf-607 / STS` and `leaf-613 / STS-2` as healthy canonical candidates
  - `dep_storm_history_inventory` narrows remaining historical storm discovery to a single `E-2 Sub 1204` bundle candidate
- Added historical-family mismatch audit tooling:
  - `scripts/maintenance/audit_historical_family_mismatches.py`
  - compares bounded historical PDF text against assigned family expectations
    such as company lineage and schedule code
  - used live to identify and purge five contaminated `nc-progress-leaf-500`
    rows that were actually Carolinas / Duke Power documents
  - now also detects rider-summary lineage mismatches where
    `SUMMARY OF RIDER ADJUSTMENTS` sheets were stored under rider-specific
    families instead of `leaf-600` / `leaf-99`
  - used live to purge stale DEP `leaf-601` summary contamination so the
    repaired `leaf-600` document could be re-mined and re-extracted under
    `progress_rider_adjustment_matrix`
  - rider/program code checks now normalize variants such as `RIDER_US_RY1`,
    `RIDER NFS-14`, and `RIDER PS`, so audit output is less noisy on
    legitimate rider leaves
  - used live to clean cross-company contamination from `leaf-535`,
    `leaf-649`, and `leaf-674`, preserving the true Carolinas HP / Rider US /
    Rider PS history under Carolinas family keys
- Added current-anchor mismatch review tooling:
  - `python -m duke_rates lineage list-current-anchor-mismatches`
  - `python -m duke_rates lineage sync-family-metadata-from-current-anchor`
  - compares `tariff_families` metadata against anchored current-document
    metadata plus mined first-page headings/leaf numbers
  - intended to surface catalog contradictions separately from parser failures
  - families whose anchor is trusted can now be synced from the anchored
    current document without direct DB edits
  - used live to sync low-risk DEP family metadata for:
    - `nc-progress-leaf-609`
    - `nc-progress-leaf-662`
    - `nc-progress-leaf-670`
  - added `python -m duke_rates lineage migrate-historical-family` so current
    leaf keys can be separated from older historical meanings without ad hoc
    DB edits
  - used live to split the remaining DEP migration-review cases into distinct
    historical-only families:
    - `nc-progress-doc-FUELCHARGEADJUSTMENT`
    - `nc-progress-doc-RESIDENTIALTIMEOFUSEENERGY`
    - `nc-progress-doc-STORMRECOVERYRIDER`
  - the live DEP current-anchor mismatch queue is now `0`
- Improved importer-side company and family matching on mined spans:
  - company inference can now use mined page text, not only filing metadata
  - descriptive rider headings such as `FUEL COST ADJUSTMENT RIDER` can now be
    normalized against abbreviated family codes such as `FUELCOSTADJRDR`
  - inline heading recovery now handles merged PDF text like
    `FUEL COST ADJUSTMENT RIDER (NC) APPLICABILITY ...`
  - mixed cover-letter plus tariff-sheet filings now classify revised/leaf
    attachment pages as `tariff` even when simple vocabulary-density scoring
    would have treated them as procedural
  - family matching can now seed span matching from filing-level hints
    (`filing_title`, mined selected/derived titles, referenced rider codes),
    which recovered additional Carolinas rider filings such as `EE` and `SCG`
  - ambiguous Duke filings now use `E-7` / `E-2` docket fallback when explicit
    filing text is too weak to distinguish Carolinas vs Progress
  - supported-family alias generation now includes Carolinas-specific rider
    long-form title aliases such as `Existing DSM Program Costs Adjustment Rider`
    and `BPM Rider`, which recovered additional live `EDPR` and
    `BPMPPTTRUEUP` filings
  - short alias matching is now boundary-aware, preventing false matches such
    as `PM` inside `BPM Rider`
  - segmentation no longer promotes cover-letter pages to `tariff` solely
    because an inline rider phrase was mined from otherwise procedural text
  - descriptive heading mining now recognizes `PROGRAM` titles in addition to
    rider/service/schedule headings
  - strong unmatched tariff spans can now create provisional historical family
    rows when no current family exists, which preserved historical-only filings
    such as `SMART ENERGY NOW PROGRAM (NC)` instead of dropping them
  - long low-signal spans now stop before family assignment or provisional
    family creation when they lack leaf numbers and schedule/rider markers,
    which prevents broad reports from reappearing as tariff documents based
    only on weak topic overlap
  - added `python -m duke_rates lineage list-weak-unbounded-historical-nc` so weak
    whole-PDF historical rows can be reviewed as a separate operator queue
    instead of being mixed into ordinary bounded-span parser work
  - the queue now classifies rows into:
    - `add_profile_or_current_parser_bridge`
    - `remine_from_discovery_record`
    - `manual_lineage_review`
  - weak legacy raw rows now infer `discovery_record_id` from stored
    regulator `local_file` metadata, which converts a meaningful share of the
    old whole-PDF backlog into a concrete `remine_from_discovery_record`
    operator path
  - added `python -m duke_rates lineage list-redundant-legacy-raw-historical-nc` so
    operators can identify legacy raw whole-PDF rows that already have bounded
    same-family regulator replacements and safely retire obsolete residue
  - historical-document upserts are more rerun-safe when the same archived URL
    is re-imported under a corrected family mapping
  - page/span artifact versions were bumped so stale cached mining artifacts can
    be selectively regenerated after heading-matching changes
  - the weak unbounded current-PDF DEP cohort has been reduced with dedicated
    current-document profiles:
    - `progress_current_leaf_bridge` now handles `leaf-501`, `520`, `535`, and `674`
    - `progress_specialty_rider` now handles `leaf-654`, `655`, `668`, and `670`
  - dependency-aware reprocessing now treats `is_current_progress_pdf` as a
    real gating signal for those current-only profiles, so profile-impact
    reruns no longer pull in unrelated historical-family rows just because the
    family key overlaps
  - single-family legacy raw attachment hints are now reused during remine, so
    discovery-backed importer runs can prefer the intended historical family
    over generic provisional placeholders when the legacy evidence is
    unambiguous
  - live cleanup has already used that path to repair and retire obsolete raw
    rows for:
    - `nc-progress-leaf-609`
    - `nc-progress-leaf-640`
    - `nc-progress-leaf-572`

Next implementation items:
- keep CPU OCR as the default path
- add optional GPU OCR/layout only for `gpu_ocr_candidate` documents
- expand historical parser profiles before adding more generic regex exceptions
- use processing-run history to skip unchanged documents unless parser/OCR versions changed
- deepen dependency rules beyond known family/profile mappings so reparsing can
  react to routing/scoring changes with less manual curation across more document classes
- refine “materially better” scoring beyond charge-count gains so weak reroutes
  can further consider charge-family expectations and document-class-specific completeness
- continue reviewing provisional historical families so historically important
  unmatched tariff programs can later be promoted into curated family models
  instead of remaining only importer-generated placeholders
- reduce summary-sheet contamination in rider families:
  - some `SUMMARY OF RIDER ADJUSTMENTS` spans are still attached to specific
    rider families like `nc-progress-leaf-601` instead of the summary family
  - this now looks like the next matching/lineage cleanup target after the
    Rider BA parser-profile work
- provisional-family review tooling is now available:
  - `lineage list-provisional-families`
  - `lineage promote-provisional-family`
  - historical-only family review tooling is now available:
  - `lineage list-historical-only-families`
  - current-document candidate suggestions are now included for historical-only
    families so review work can start from likely anchors instead of manual scans
  - candidate scoring now also uses first-page mined current-document evidence
    such as headings and leaf numbers when document metadata is too thin
  - candidate scoring now also uses historical leaf continuity to suppress
    wrong-leaf current-document suggestions when a historical family already
    has reliable leaf evidence
  - historical-only family review now exposes:
    - `review_candidates` for families with plausible anchors
    - `unresolved` for families with no current candidates
    - `--only-unresolved` filtering in the operator CLI
  - `lineage attach-current-document-to-family` now exists for confirmed anchor linkage
- live follow-through completed:
  - `nc-carolinas-program-SMARTENERGYNOWPROGRAM` has already been promoted from
    provisional placeholder status into the curated family catalog

### 2c. Document intelligence and parser-learning layer — PLANNED

Goal: make the parsing pipeline reusable across wider document classes, regulators,
and states without turning it into one giant rule file.

Planned capabilities:
- persist a reusable `document_fingerprint` / span-fingerprint feature record
- log parse attempts, parser profile selection, confidence, review flags, and outcomes
- retain more mined evidence from already-downloaded documents instead of only final extracted fields
- build a document-relationship map so non-tariff filings can still be linked to:
  - tariff families
  - rider codes
  - specific revisions / effective dates
  - dockets and sub-dockets
  - related time windows
  - supporting evidence topics such as fuel cost recovery, DSM, purchased power, or decoupling
- add an LLM-assisted analysis layer for:
  - document-purpose explanation
  - richer document fingerprinting / classification
  - relationship discovery across filings
  - extraction-target suggestion for documents not yet parsed
  - human-readable summaries of how and why a document matters
- build a gold-set evaluation corpus for representative document classes
- use diagnostics to decide when to:
  - refine an existing parser profile
  - create a new profile
  - improve OCR/table routing
  - keep additional metadata from existing PDFs

Implementation items:
- completed foundations:
  - `DocumentFingerprint` storage
  - `parse_attempt_log` and `review_outcome` storage
  - manual review write paths
  - reporting on weak profiles / families
  - correction-category summaries
  - targeted historical reprocess queue and versioned processing runs
  - OCR queue + OCR artifact cache foundations
  - page/span artifact persistence and reuse
  - stage-aware invalidation/versioning foundations
- remaining implementation items:
- add profile fallback sequencing and profile recommendation rules
- add a `document_relationships` / relationship-index layer so downloaded but
  currently unparsed documents can still be traced back to riders, rates, dockets,
  timeframes, and later re-digested when new extraction goals appear
- add an `llm_document_analysis` / `llm_document_insights` layer that stores:
  - document-purpose summaries
  - suggested document classes / topics
  - candidate rider/rate/docket/timeframe links
  - extraction opportunities not covered by current parsers
  - evidence snippets and confidence so LLM output remains auditable
- create a labeled evaluation set for:
  - native tariff sheets
  - compliance books
  - scanned tariffs
  - rider matrices
  - legacy Duke Power / Carolinas documents
  - ambiguous procedural filings
- add reporting to identify high-failure document clusters and under-captured evidence
- use `historical_processing_runs` + `historical_reprocess_queue` to target
  reparsing of changed or weak documents instead of full archive sweeps
- keep deterministic extraction as the source of truth; use LLM output as an
  assistive layer for classification, explanation, linking, and future parser targeting

### 2d. Retire required JSON handoff for NCUC ingest — PLANNED

Goal: keep JSON export available for audit/debug, but stop requiring JSON files
 as the normal interface between parsing and persistence.

Current state:
- `ingest-ncuc` writes `ingest_results.json` / `rider_summaries.json`
- `load-ncuc-ingest` reads those JSON files into `ncuc_ingest_segments`,
  `rider_summary_blocks`, and `rider_line_items`

Target state:
- parse directly into DB-backed records during normal ingest
- keep optional `--export-json` output for reproducibility and inspection
- reduce drift between intermediate files and DB state
- reduce `data/` clutter from retained run snapshots

Implementation items:
- add direct DB writers for `IngestResult` and rider-summary outputs
- refactor `load_ingest_results()` / `load_rider_summaries()` logic into reusable
  insert/update helpers that can be called from both JSON and in-memory flows
- keep `load-ncuc-ingest` as a compatibility command during transition
- add a single command path that can parse and persist in one run
- mark JSON artifacts as optional exports rather than required workflow state

### 2e. Agent operations and handoff scaffolding — DONE 2026-03-26

- Added a root-level agent contract in [AGENTS.md](/c:/Python/Duke/Standalone/AGENTS.md)
- Added explicit task-routing guidance in
  [agent_task_routing.md](/c:/Python/Duke/Standalone/docs/agent_task_routing.md)
- Added source-of-truth vs legacy-path guidance in
  [source_of_truth_and_legacy_paths.md](/c:/Python/Duke/Standalone/docs/source_of_truth_and_legacy_paths.md)
- Added a consistent change/handoff checklist in
  [agent_change_checklist.md](/c:/Python/Duke/Standalone/docs/agent_change_checklist.md)
- Updated operator docs so multiple AI agents can pick up work with less repo
  reverse engineering and less workflow drift

### 3. Broaden beyond residential NC — DONE 2026-03-22

- ~~extend the cleaned-history and billing-quality path to:~~
  - ~~NC residential TOU families beyond the current bill-tested path~~ — DONE
    - DEP TOU residential (R-TOU, R-TOUD) covered via `"Residential Service Schedules"`
      rate class — all DEP residential schedules share the same Leaf 600 summary page.
      Already captured by `load_dep_res_canonical_rider_components()`.
  - ~~NC small commercial schedules where data quality is good enough~~ — DONE
    - `load_dep_sgs_canonical_rider_components()` — DEP SGS + SGS-TOUE
      (`"Small General Service Schedules"` rate class, 2023-10+, 11 rider codes including
      commercial-only BA-EE)
    - `load_dep_sgs_clr_canonical_rider_components()` — DEP SGS-TOU-CLR
      (`"Small General Service - Constant Load Schedule"` rate class, 2023-10+)
  - `rider_trust.py` extended: now covers 4 rate class groups
    (`dep_residential`, `dep_sgs`, `dep_sgs_clr`, `dec_residential`).
    `rate_class_group` column added to trust table; continuity scoped per group.
  - Trust table row count: ~500+ rows (up from 172 residential-only)
  - Tests: 345 passing (up from 323)
- ~~prerequisite: `utility` column~~ — DONE (TD-001, 2026-03-21)
- Next: broader state analytics, UI polish, EIA deferred items

### 4. UI polish — DONE 2026-03-22

- ~~Rider Trust section added to `streamlit_res_comparison_app.py`~~
  - Trust tier KPI tiles (color-coded: high/medium/low/unverified)
  - Expandable "Trust score detail by rider" table (mean scores per group)
  - Expandable "Trust tier counts by rate class group" pivot
  - Expandable "Confidence Guide" explaining the scoring model
  - Filters by `rate_class_group` and `trust_tier`
- Remaining: dedicated bill-validation dashboard (deferred, low priority)

### 5. Broader generalized tariff schema

The originally planned fully generalized tariff-version schema is still valuable, but it is no
longer the immediate blocker for useful work. If resumed, it should be informed by the working
NC residential path rather than designed in abstraction first.

**Note (updated 2026-03-21):** `tariff_families` / `tariff_versions` / `tariff_charges` ARE the
active Phase 4a billing path used by `src/duke_rates/billing/tariff_engine.py`. They contain
554 families and 633 charges across all 7 state/company combinations. The legacy
`ncuc_ingest_segments` / `rider_summary_blocks` path is the NC residential DEP/DEC-specific
analytics path. Both paths are operational. See `schema.py` block comment for details.

## Completed Phases (2026-03-22)

All six original project phases are now complete:

| Phase | Description | Status |
|-------|-------------|--------|
| 2 | Classification sweep + rev-token detection + versioned schema | DONE |
| 3 | NC Progress tariff parsing + charge ingestion | DONE |
| 3b | SGS/TOU broadening + canonical rider/trust beyond residential | DONE |
| 4 (UI) | Rider Trust section in rate comparison Streamlit app | DONE |
| 4a | Billing engine + rate comparison CLI + Streamlit rate comparison app | DONE |
| 4a+ | ESPI/Green Button XML parser + shift simulator + TOU upload | DONE |
| 5 | Solar/storage sizing — `solar_sizing.py` + Solar Sizing tab in Streamlit | DONE |
| 6 | OpenEI/URDB export — `urdb_export.py` + `duke-rates export-urdb` CLI | DONE |

Current test count: **472 passing**.

## Post-Phase 6 Additions (2026-03-22)

### Tariff update checking + NCUC rate pipeline monitor

- `duke-rates tariff-update` CLI command: crawls Duke Energy website, compares
  each discovered document against the DB via `rev_token` and `content_hash`,
  and reports each as NEW / CHANGED / UNCHANGED.  `--dry-run` for inspection
  only; `--auto-parse` chains into `parse-tariff-versions` automatically for
  any companies with new or changed documents.

- `duke-rates ncuc-pending-rates` CLI command: reads the local
  `ncuc_discovery_records` table and surfaces filings by category:
  APPLICATION/SETTLEMENT (proposed rate changes not yet approved), recent
  ORDERs (approved changes that may need re-parsing), TARIFF_SHEETS, and
  rider-related filings.  Requires `ncuc-seed-discover` or `ncuc-search` to
  have populated the table first.  `--json` output for scripting.

- Streamlit rate comparison app: new "Data Updates" section in sidebar showing
  last crawl date, document count, and parsed family coverage for the selected
  utility.  Shows a warning when data is older than 30 days.  Includes
  copy-paste CLI hint for running an update.

## Billing Engine Improvements (from version_4 comparison, 2026-03-23)

Identified by comparing the Standalone engine against the `version_4` home energy tool.
Full detail (problem, acceptance criteria) in [docs/technical_debt.md](technical_debt.md)
under "Billing Engine — Lessons from version_4".

| Item | Description | Priority |
|------|-------------|----------|
| TD-V4-001 | Rider total cross-check against leaf-600 Summary total — catch rate data errors early | High |
| TD-V4-002 | Segment-level billing breakdown in `BillResult` — make mid-period rate changes auditable | Medium |
| TD-V4-003 | Verify CEPS ($0.39/month) coverage in leaf-500 charge rows — may be missing fixed charge | Medium |
| TD-V4-004 | Parse R-TOUD demand charges (leaf-501) — currently excluded from rate comparisons | Low |

### Optional Rider Support (identified 2026-03-23)

Duke riders are either mandatory (every customer on the schedule pays them) or optional
(opt-in, conditional on customer circumstances, or geographic).  Currently all
`rider_applicability` rows have `mandatory=1` and optional riders are entirely absent
from the DB.  Full detail in [docs/technical_debt.md](technical_debt.md) under
"Optional Rider Support".

Implement in order:

| Item | Description | Priority |
|------|-------------|----------|
| TD-OPT-001 | Add `enrollment_type` column to `rider_applicability`; insert opt-in/conditional rider links for NC Progress residential | High |
| TD-OPT-002 | Add `extra_riders` parameter to `TariffBillingEngine.calculate()` — lets callers include specific optional riders per estimate | High |
| TD-OPT-003 | Add optional rider toggles to Streamlit rate comparison sidebar — checkboxes grouped by enrollment type | Medium |

**Why enrollment_type over a simple flag:**
Optional riders vary in *kind*, not just presence.  RECD is an opt-in discount (customer
enrolls once and stays enrolled).  NM is conditional on having solar installed.  A future
geographic rider might only apply in a specific service district.  `enrollment_type`
captures that distinction so the UI can present them correctly (e.g., "opt-in programs"
vs. "requires equipment") without hardcoding rider keys in application logic.

**Geographic scope:**
No confirmed geographic-only residential riders exist for NC Progress today.  When one
is identified, the `applicability_notes` field on `rider_applicability` is the holding
place for plain-text territory descriptions.  A structured `geographic_scope` column
can be added at that time.

**Key finding from version_4 analysis:**
The "Energy Conservation Credit" on Duke bills is leaf-640 RECD — a 5% energy discount
for Energy Star certified homes, opt-in only.  Version_4's `bill_forecast_res.py` incorrectly
applies RECD universally (as -$0.00606/kWh) causing a systematic ~$8/month underestimate for
non-enrolled customers.  Our Standalone engine correctly excludes RECD from general residential
calculations.  The ~$8/month difference between Standalone estimates and the specific test
customer's bills is explained entirely by RECD (confirmed: 5% × kWh × $0.12119/kWh = exact match).

## Deferred / Longer-Term

### Historical OCR and complex PDF handling

- Implement CPU-first OCR reintegration for scanned NCUC PDFs
- Add a separate OCR queue instead of mixing OCR directly into the importer
- Add optional GPU-backed OCR/layout for triage-flagged edge cases
- Pilot Docling as an optional OCR/layout/table backend for hard PDFs
- Compare Docling CPU vs CUDA on a narrow validation sample before deciding
  where it belongs in the operational pipeline
- Persist Docling JSON / text / table artifacts separately from plain OCR sidecars
- Add stronger table extraction for native and scanned table-heavy pages
- Cache OCR outputs and page artifacts to avoid expensive reruns

### Document intelligence and evidence retention

- Add persistent document / span fingerprints so routing decisions are feature-driven
- Preserve more mined evidence from downloaded PDFs for future reprocessing:
  - headers and footers
  - candidate dates
  - tables and layout cues
  - OCR artifacts
  - schedule, rider, and leaf candidates
  - parser review flags and failure reasons
- Build a parse-attempt history so parser improvements can be driven by actual failure patterns
- Add a gold-set regression corpus so parser changes are measured, not guessed
- Generalize the pipeline so other states and regulators can plug in via new feature maps and parser profiles

### Multi-state structured parsing

- FL / IN / KY / OH / SC tariff families are registered but charge data is sparse
- They should be treated as crawl/archive/parser baselines, not validated analytical products
- Run `duke-rates eia-backfill --states NC SC VA TN GA US` for full Southeast EIA coverage

### Solar capacity factors for non-NC states

- `solar_sizing.py` currently only has NC capacity factors (NREL PVWatts Raleigh)
- SC, FL, IN, OH, KY factors should be added when solar analysis is needed for those states
- SC and FL will produce higher annual generation; IN/KY will produce lower

### Duke NC Net Metering Rider NM/NMB charge parsing

- `nc-progress-leaf-641` (Rider NM) and `nc-progress-leaf-669` (Rider NMB) are registered
  families but have no parsed charge rows yet
- Parsing would enable exact solar avoided-cost rate from DB instead of the hardcoded $0.04/kWh default
- Required when Duke transitions from retail net metering to avoided-cost billing

### Historical rate comparison — year-over-year bill escalation

- The canonical residential timeline has multi-year rate data
- A natural next feature: given actual usage, show what the bill would have been in each prior year
- Answers: "how much has my bill gone up since 2018, holding usage constant?"
- Requires wiring `TariffBillingEngine` to `tariff_versions` effective-date lookup across years

### EIA facility-level data (EIA-006)

- Plant-by-plant generation from `electricity/facility-fuel`
- Relevant for data-center load growth analysis; very large dataset
- Deferred — not needed for state-level analysis

---

## EIA National Context Integration — Implemented 2026-03-21

A new EIA Open Data API v2 integration layer has been added as a complementary module
alongside the existing Duke tariff path.  It does not replace or modify the tariff/rider
workflows.

### Why EIA integration matters

The Duke tariff analysis explains **how** Duke bills customers.  EIA data explains **where
Duke customers stand relative to the country** — it is the national context layer that turns
tariff mechanics into meaningful policy and consumer-facing insights.

Key questions it unlocks:
- Is NC electricity cheap or expensive relative to the US, the Southeast, and peer states?
- How has the price gap between NC and the national average changed over time?
- Do states with different market structures (regulated vs. restructured) actually have
  different price outcomes — and what caveats apply?
- What share of NC's generation comes from gas, coal, nuclear, and renewables, and how has
  that changed?
- Which Duke-served states are outliers in cost or fuel mix?

**Causality caution**: EIA data is strong for describing prices, sales, and generation.
It does not explain *why* prices differ.  Regulation, capital costs, geography, transmission,
fuel access, and policy all matter independently.  All analysis in the EIA layer is explicitly
framed as observational/descriptive, not causal.

### What was built

**New package: `src/duke_rates/eia/`**
- `client.py` — EIA API v2 HTTP client with pagination, retry, and local caching
- `endpoints.py` — endpoint-specific fetch functions (retail sales, generation, profiles, etc.)
- `transformers.py` — normalize raw API strings to typed Python dicts
- `loaders.py` — idempotent SQLite upsert functions for each EIA table
- `references.py` — static lookup tables: census regions, market structure, RTO affiliation

**New database tables (added via `schema.py:migrate()`)**
- `eia_retail_sales` — price, sales, revenue, customers by state/sector/period
- `eia_generation_by_fuel` — net generation by state, fuel type, sector
- `eia_state_profile_summary` — annual state rankings (2008+)
- `eia_source_disposition` — supply/disposition balance (1990+)
- `eia_state_capability` — net summer capacity by state and energy source
- `eia_state_region_lookup` — census division/region reference
- `eia_market_structure_lookup` — regulated/hybrid/restructured + RTO reference

**Scripts**
- `scripts/eia_backfill.py` — one-shot historical backfill (all states, 2001+)
- `scripts/eia_incremental_update.py` — monthly incremental update

**CLI commands** (requires `EIA_API_KEY` in `.env`)
- `duke-rates eia-backfill` — full historical backfill
- `duke-rates eia-update` — incremental update
- `duke-rates eia-state-price NC` — price history for a state
- `duke-rates eia-national-comparison 2024` — national price ranking table

**Analytics: `src/duke_rates/analytics/eia_analytics.py`**
- `load_price_history()` — price time series by state/sector
- `load_state_vs_national()` — price delta vs US average with YoY change
- `load_fuel_mix_shares()` — generation fuel mix shares by state and year
- `load_price_vs_fuel_mix()` — joined dataset for scatter analysis
- `load_price_rankings()` — annual state price ranking table
- `load_duke_state_context()` — Duke-served states vs US benchmark
- `load_southeast_comparison()` — Southeast region trend
- `load_market_structure_comparison()` — median price by market structure

**Streamlit app: `streamlit_eia_app.py`**
- Southeast Trends tab
- National Rankings tab (bar chart + sortable table)
- Fuel Mix tab (stacked area / bar)
- Price vs Fuel Mix scatter (with trendline; observational caution displayed)
- Market Structure comparison
- Duke State Context tab

### How to get started with EIA data

```bash
# 1. Ensure EIA_API_KEY is set in .env
echo "EIA_API_KEY=your-key-here" >> .env

# 2. Run the backfill (all 50 states, ~5-10 minutes first time)
duke-rates eia-backfill

# OR: start with just the Southeast states (faster)
duke-rates eia-backfill --states NC SC VA GA TN FL IN OH KY

# 3. Open the Streamlit app
streamlit run app/streamlit_eia_app.py

# 4. Run monthly updates going forward
duke-rates eia-update
```

### EIA data API key

Register at https://www.eia.gov/opendata/register.php (free, instant).
Set `EIA_API_KEY=<your-key>` in `.env`.

### Deferred EIA items (backlog)

See `docs/technical_debt.md` section "EIA Integration Deferred Items" for tracked
follow-on work including monthly generation ingestion, facility-level data,
Duke+EIA revenue reconciliation, and weather normalization.

### Duke + EIA integration path

The primary integration point is the Streamlit comparison app.  The current EIA layer
provides state-average context.  Future integration steps:

1. Overlay Duke billing-engine estimates (per-kWh) on EIA state-average trend lines in
   the same chart — shows how Duke residential compares to the NC state average.
2. Compare Duke's per-kWh revenue (from tariff + rider data) to EIA's revenue-per-kWh
   (revenue / sales) to detect divergence between what Duke charges and what EIA reports.
3. Annotate the canonical residential timeline with "NC state average" from EIA — gives
   every rate-change event a national-context annotation.
4. Add a "NC vs US trend gap" indicator to the comparison app header.

---

## Phase 6b: Docling Integration (complete 2026-03-27, bridge 2026-03-27)

Docling backend and artifact caching completed in previous session. Now the bridge
from stored Docling artifacts into the page-aware parsing pipeline is complete.

The bridge reconstructs `PageEvidence` from stored Docling JSON, segments into
`TariffSpan`s, matches to families, and creates `HistoricalDocumentRecord`s using
existing pipeline logic. Docling artifacts feed into the same family matcher,
parser registry, and reprocess queue as native-text and OCR paths — not a separate
parsing architecture.

### Backlog Composition

| Classification | Count | Priority | Accelerator |
|---|---|---|---|
| tariff_sheets | 585 | 1 (structured documents) | CUDA |
| order | 467 | 2 | CUDA |
| other | 1,777 | 3 (low-value, sampled) | CPU |
| notice | 36 | 2 | CUDA |
| testimony | 5 | 2 | CUDA |
| exhibit | 13 | 2 | CUDA |
| application | 33 | 2 | CUDA |
| attachment | 17 | 2 | CUDA |
| compliance_filing | 5 | 2 | CUDA |
| settlement | 1 | 2 | CUDA |

### Docling Artifact Conversion Status

**Phase 1 (complete):** Tariff sheets on GPU
- Command: `process-docling-batch --accelerator cuda --classification tariff_sheets`
- Result: 546/585 documents processed (93.3%)
  - 466 successful (79.5%)
  - 80 partial success with fallback (13.7%)
  - 39 remaining (6.7%, very large PDFs causing OOM)
- Storage: DB-first (docling_artifacts table)
- Speed: 1-26 sec per document depending on page count and table density

**Phase 2 (active):** Remaining oversized tariff sheets on CPU
- Processing 39 documents that caused GPU OOM
- Command: `process-docling-batch --accelerator cpu --classification tariff_sheets --limit 39`

**Bridging Docling artifacts into page-aware pipeline (new):**
- Command: `mine-docling-nc --limit 50 --accelerator cuda` (or cpu)
- Reconstructs `PageEvidence` from stored Docling JSON
- Segments into `TariffSpan`s using existing `segment_document()`
- Matches to families using existing `find_best_family_for_span()`
- Creates `HistoricalDocumentRecord`s via existing repository logic
- Reuses existing family matcher, parser registry, and extraction path

### How Docling Fits The Pipeline

Per `docling_integration_plan.md`:
- **Not** a replacement for deterministic extraction
- **Optional** heavy-analysis backend for:
  - OCR-heavy scans
  - Table-heavy documents (rider summaries, compliance books)
  - Weak or empty historical parses
  - Future LLM-assisted analysis
- **Feeds into** existing pipeline:
  - importer → page miner → family matcher → parser profiles → reprocess queue → review
- **Architecture:** Backend wrapper with file-hash cache, DB artifact storage, dispatcher routing

### Monitoring

Check progress with:
```bash
python -c "
import sqlite3
conn = sqlite3.connect('data/db/duke_rates.db')
done = conn.execute('SELECT COUNT(*) FROM docling_artifacts WHERE status = \"success\"').fetchone()[0]
total = conn.execute('SELECT COUNT(*) FROM ncuc_discovery_records WHERE local_path IS NOT NULL').fetchone()[0]
print(f'{done}/{total} documents processed')
conn.close()
"
```

### Next Steps

1. Complete Phase 2 CPU batch for oversized tariff sheets (39 docs)
2. Bridge Phase 1 + 2 artifacts into page-aware pipeline:
   - `python -m duke_rates mine-docling-nc --limit 50 --accelerator cuda`
   - Monitor historical document creation: `python -m duke_rates parse-review-summary`
3. Evaluate extraction quality and family matching accuracy
4. (Future) Phase 3 + 4: Process orders (467) and sample "other" (1,777) documents

---

## Phase 7: Regulatory Intelligence Layer

The regulatory intelligence layer moves beyond tariff data extraction into the
causal and political context of rate decisions. The goal is to answer not just
*what* rates changed, but *why* — and to surface patterns across people,
money, and policy that are not visible in the tariff documents themselves.

### Vision

Build a provenance-aware graph that connects:

```
campaign contribution → commissioner → docket vote → rate increase → tariff version
      ↑                       ↑
  PAC source              employment history
  (NCSBE/FEC)             (utility → regulator revolving door)
```

Every link carries: source_pdf, evidence_text, confidence, extraction_method —
so the chain of evidence can be audited back to primary sources.

### Schema (implemented)

**INTEL-001** (`src/duke_rates/db/schema.py`):

| Table | Purpose |
|---|---|
| `decision_makers` | People who appear in NCUC proceedings (commissioners, witnesses, attorneys, ALJs) |
| `docket_appearances` | Each time a person appears in a document, their role, and evidence text |
| `docket_outcomes` | Commission orders: outcome type, rate change %, revenue impact, effective date |
| `tariff_causal_links` | Bridges docket outcomes to tariff versions (approved_by, modified_by, etc.) |
| `entity_relationships` | Open-ended subject→relationship→object triples for any pattern not yet modeled |
| `document_type_registry` | Self-describing registry of ingested document types and extraction maturity |

**INTEL-002** (`src/duke_rates/db/schema.py`):

| Table | Purpose |
|---|---|
| `financial_relationships` | Campaign contributions, PAC money, honoraria, board compensation, legal retainers |
| `employment_history` | Revolving-door tracking: utility → regulator → lobbyist career trajectories |
| `legislative_actions` | NC General Assembly bills affecting rate-setting authority, IRP, renewables, cost recovery |

### Data sources (external)

| Source | Data | Access |
|---|---|---|
| NC Board of Elections (NCSBE) | Campaign finance: contributions to commissioners, legislators | `https://cf.ncsbe.gov/CFOrgLkup/` bulk CSV |
| FEC / OpenSecrets | Federal PAC contributions, Duke Energy PAC history | FEC bulk data, OpenSecrets API |
| SEC EDGAR | Duke proxy statements: board compensation, executive employment | EDGAR full-text search |
| NC General Assembly | Bill text, sponsors, vote records | `https://ncleg.gov` bill API |
| Ballotpedia | Commissioner profiles, partisan affiliations | Web |
| NCUC commissioner bios | Current/former commissioner employment history | `https://www.ncuc.net` |

### Chain of evidence example

A complete causal chain the schema can represent:

1. Duke Energy PAC contributes $X to Commissioner Y's campaign (2018 election cycle)
   → `financial_relationships` row, source: NCSBE filing
2. Commissioner Y previously worked at Duke Energy as VP Regulatory Affairs (2010–2016)
   → `employment_history` row, source: NCUC bio / LinkedIn
3. Commissioner Y votes yes on E-2 Sub 1206 rate case (2019)
   → `docket_appearances` row (appearance_role: signing_commissioner) + `entity_relationships` (voted_yes)
4. E-2 Sub 1206 approves 12.4% residential rate increase
   → `docket_outcomes` row
5. Leaf No. 500 rate schedule updated effective 2019-06-01
   → `tariff_causal_links` row (link_type: approved_by)
6. Tariff charges extracted from Leaf 500 v.2019-06-01
   → `tariff_charges` rows (existing pipeline)

### Extraction approach

**Phase 7a (NLP / regex, no training data needed):**
- Extract commissioner names and signatures from order PDFs via regex
- Extract testimony witness names and affiliations from hearing transcripts
- Parse NCSBE bulk CSV directly into `financial_relationships`
- Parse NC General Assembly bill search results into `legislative_actions`

**Phase 7b (NER model, needs labeled data):**
- Fine-tune a spaCy / Hugging Face NER model on NCUC document types
- Entities: PERSON, ORG, DOCKET_NUMBER, DATE, RATE_SCHEDULE, DOLLAR_AMOUNT
- Training data: ~200 labeled NCUC documents (orders, testimonies, settlements)
- Feeds into `decision_makers` / `docket_appearances` with `extraction_method = 'ner_model'`

**Phase 7c (graph analytics):**
- Revolving-door detection: `employment_history` overlap with `docket_appearances` date ranges
- Concentration metrics: which law firms represent Duke vs. which represent intervenors
- Commissioner voting pattern analysis: Yes/No/Recuse rates by docket type
- Campaign contribution timing relative to rate case filing dates

### CLI commands (planned)

```bash
# Ingest NCSBE campaign finance CSV
duke-rates ingest-campaign-finance --source ncsbe --cycle 2022

# Import NC General Assembly bills for a session
duke-rates ingest-legislation --session 2023-2024 --category utility

# Extract decision-makers from a batch of NCUC order PDFs
duke-rates extract-decision-makers --docket-dir data/ncuc/E-2/

# Show chain of evidence for a rate change
duke-rates trace-rate-change --tariff-version-id 42

# Export influence graph for a docket
duke-rates export-influence-graph --docket "E-2 Sub 1206" --format dot
```

### Relationship to existing pipeline

The regulatory intelligence tables are additive — they reference existing tables
(`tariff_versions`, `tariff_families`, `documents`) by natural key but do not
modify them. The Docling pipeline (Phase 6) is the primary source of PDF text
that NLP extraction will consume. The triage pipeline's `document_type` signal
determines which documents go into each INTEL extraction path.

---

## Recommended Sequencing for New Work

1. Apply the structural correctness fixes in section 0 above (utility column, uniqueness
   constraint, season consolidation, timestamp fix) — these are low-risk, high-leverage
2. Preserve and improve the current NC residential validated path
3. Clean the rider-component raw layer
4. Build clearer trust / QA exports
5. Expand horizontally to adjacent validated use cases
6. Revisit the generalized schema only after the practical path remains stable

## Handoff note for AI agents

If you are picking up work on this project, read [docs/technical_debt.md](technical_debt.md)
before making schema or billing-engine changes. It contains precise file references,
reproduction steps, and acceptance criteria for each open structural issue. The issues in
section 0 above must be resolved before expanding to new utilities or states.

For the general multi-agent operating contract and read order, start with
[AGENTS.md](/c:/Python/Duke/Standalone/AGENTS.md).
