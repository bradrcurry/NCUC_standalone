# Phase 2A: Non-Destructive Extraction, Provenance and Billing Safety

**Date:** 2026-10-27
**Branch:** `phase2a/provenance-billing-safety`, cut from `stabilize/phase0-docling-rider-fixes` at `25def84`. Local only; not pushed or merged.
**Method:** Production (`data/db/duke_rates.db`) was opened read-only throughout. All migrations, backfills, promotions and live tests ran on a scratch copy made with the SQLite backup API. Production's SHA-256 before and after is in §10.

---

## 1. Summary

| Definition of done | Status |
|---|---|
| New extractions can be stored without replacing previous results | **Done.** `extraction_runs` + `extracted_charges`/`extracted_documents`. Runs are append-only, and sealed runs are immutable (enforced by triggers). `provenance extract-candidate` runs the production extractor into a candidate run; `tariff_charges` is not touched. |
| Candidate charges can be compared against active records | **Done.** `compare_run` diffs a run against what billing reads today. It reports added, removed, value, unit, effective-date, rate-class, applicability and provenance differences, and marks removals of curated rows. |
| Promotion and rollback are tested | **Done.** Promotion is explicit, atomic and logged. Rollback restores the replaced rows with their original ids, byte for byte, and refuses if the data changed after the promotion. Covered by unit tests and an end-to-end run on the full-size scratch copy (§6). |
| Source provenance is recorded where available | **Done.** Per charge: source PDF and SHA-256, page, bbox, quote, printed and canonical unit, rate class, applicability, effective date, method and validation. Unknown fields are stored as `unavailable`. CHECK constraints reject a value without a status, and a status without a value. |
| Historical tariff metadata changes are auditable | **Done.** Triggers record every change to audited columns, including writes from the 31 ad-hoc scripts, with old value, new value, time and origin. `revise_metadata` adds attribution and evidence; `revert_metadata` undoes a change. |
| Future-effective tariffs cannot silently price earlier service | **Done.** `TariffBillingEngine` and the historical selector both refuse. They return an explicit incomplete-coverage result (§4). |
| Unsupported potentially billable charges are not silently ignored | **Done.** The engine reports them in `BillResult.unsupported_charges` and in warnings. Totals are unchanged (§5). |
| Existing billing baseline remains valid | **Done.** See §7. |
| Production data remains unchanged | **Done.** See §10. |

The provenance schema is **opt-in**. `connect()` does not install it, so no existing database changes until someone runs `duke-rates provenance migrate` on it. Production has **not** been migrated.

## 2. Files changed

| File | Change |
|---|---|
| `src/duke_rates/db/provenance_schema.py` (new) | DDL for 13 tables. Immutability triggers, audit triggers, and an idempotent `ensure_provenance_schema`. `drop_provenance_schema` is the migration rollback; it refuses to discard history unless forced. |
| `src/duke_rates/db/provenance.py` (new) | Write API: `start_run`, `record_charges`, `record_document`, `seal_run` (validation), `compare_run`, `promote`, `rollback`, `reject_run`, `add_override`/`retire_override`, `revise_metadata`/`revert_metadata`, `metadata_history`, `payload_history`, `backfill_legacy`, `verify_materialization` and `provenance_completeness`. |
| `src/duke_rates/historical/ncuc/pipeline/candidate_runs.py` (new) | `extract_candidate`: runs `BulkExtractor.extract_charges_from_document` and stores a sealed candidate run. It does not write `tariff_charges` or the classification tables. |
| `src/duke_rates/cli_commands/provenance.py` (new), `src/duke_rates/cli.py` | `duke-rates provenance` commands: `migrate`, `drop-schema`, `backfill`, `verify`, `extract-candidate`, `compare`, `promote`, `rollback`, `reject`, `history`, `completeness` and `unsupported-charges`. Write commands require `--db` and refuse the production DB without `--allow-production`. |
| `src/duke_rates/billing/charge_types.py` (new) | Classifies stored `charge_type` values. Detects `¢` units that the engine prices as dollars. |
| `src/duke_rates/billing/tariff_engine.py` | `select_version_with_coverage` (new). `_select_version` returns `None` for future-only families. `BillResult.coverage_status` and `unsupported_charges` (new fields, with defaults). Adds coverage, unsupported-charge and cent-unit warnings on the base and rider paths. |
| `src/duke_rates/historical/tariff_selector.py` | `IncompleteTariffCoverageError` (a `ValueError`), raised instead of selecting a future version. `HistoricalTariffSelection.selection_basis` (new field, with a default). |
| `tests/test_extraction_provenance.py` (new) | 28 tests: migration and rollback, backfill, immutability, provenance statuses, validation, comparison, promotion and rollback, overrides, payload versioning, metadata audit. |
| `tests/test_billing_safety.py` (new) | 32 tests: coverage, date boundaries, overlapping and expired versions, engine incomplete-coverage results, unsupported charges, cent units, classification of all 13 production types. |
| `tests/test_historical_tariff_selector.py` | +2 tests: refusal before the first version, and date boundaries. |
| `tests/test_tariff_engine.py` | `test_future_only_returns_earliest` → `test_future_only_returns_none`, an intentional behaviour change. Four fixture versions were back-dated (see §4.3). |
| `reports/phase2a/*` | Evidence: test logs, scratch migration report, benchmark run and scores, and the unsupported-charge classification. |

## 3. Schema additions

All additive. No existing table or column is altered.

| Table | Purpose |
|---|---|
| `extraction_runs` | One row per extraction attempt. Records: `run_uuid`; target (`tariff_version` / `historical_document` / `bill_statement`); family and version; source document id, path and SHA-256 (with `computed` / `stored_unverified` / `unavailable` status); parser name and version (git SHA, plus `-dirty`); code fingerprint (hash of producer modules); model id and digest; config JSON and fingerprint; text source; method; interpreted effective dates; start and finish timestamps; `status`; `validation_status` and `validation_json`; metrics. Immutable once `sealed_at` is set. |
| `extracted_charges` | Charge rows of a run. Records: value; stored, printed and canonical unit, plus the normalisation applied; tiers, TOU, season, rate class; `applicability_json`; effective dates; source PDF and SHA-256; page; bbox; quote; extractor snippet; each provenance field's status; method; confidence; natural key. Legacy rows also carry `legacy_tariff_charge_id` and the exact `legacy_row_json`. |
| `extracted_documents` | Immutable `parsed_result_json` / `statement_json` payloads with their SHA-256. |
| `active_results` | The live run per target. |
| `promotion_log` | Records every `activate_legacy` / `promote` / `rollback` / `reject`: diff, before/after state hashes, deleted rows (full), inserted ids, preserved and superseded ids. |
| `materialized_charges` | Which `tariff_charges` row a promotion created, and from which extracted charge or override. |
| `charge_overrides` | Curated `add` / `replace` / `suppress` entries applied on promotion. Each records its source (`correction` / `manual` / `llm`), reference, evidence and approver. Rows can only be retired, never edited or deleted. |
| `metadata_revisions` | Column-level history for `historical_documents` (dates, family, revision and supersedes labels, leaf, status, bindings, hash, path, page span), `tariff_versions` (dates, family, document and historical-document bindings, labels, status, approved version) and `tariff_families` (key, current document). Row deletes are recorded too. |
| `document_payload_revisions` | The previous value of every overwritten or deleted `parsed_result_json` / `statement_json`. |
| `tariff_charge_revisions` | Every insert, update and delete on `tariff_charges`, with the full old and new row. A destructive re-extraction can be reconstructed from it. |
| `provenance_write_context` | Single-row attribution context. Explicit APIs set it inside their transaction; anything else is recorded as `unattributed`. |
| `document_blobs`, `provenance_schema_meta` | Source bytes seen (by hash), and the schema version. |

Triggers (52 on the production schema): 8 append-only guards, 5 immutability guards, 3 sealed-run guards, 2 limited-update guards (`metadata_revisions.reverted_by`, override retirement), 31 metadata/payload audit triggers (one per audited column, plus row-delete and payload triggers) and 3 `tariff_charges` audit triggers.

## 4. Historical version selection

### 4.1 Defect and fix

| Path | Before | After |
|---|---|---|
| `TariffBillingEngine` (`_select_version`) | Every version starts after the service date → the **earliest future** version was used, with no warning. | Returns no version. `BillResult.coverage_status="incomplete"`, total 0, and the warning names the earliest effective date. |
| Engine rider path | The same future fallback applied to riders. | Future-only riders are excluded and listed in an "Incomplete coverage" warning. Previously they were either priced from the future version or reported as "not yet parsed". |
| `ProgressNCHistoricalTariffSelector.select_schedule` (12-bill path) | Tier 1 of `_selection_score` (future versions) won when nothing earlier existed. | Raises `IncompleteTariffCoverageError`. It subclasses `ValueError`, so the CLI and `bill_accuracy` keep reporting it per bill. `selection_basis` records `covering` / `latest_prior` / `undated`. |
| Expired latest version (`effective_end` < service date, nothing newer) | Used silently. | Still used, with a warning (`coverage="expired"`). |
| Undated versions | Used silently. | Still used, with a warning (`undated_fallback`). |

### 4.2 Mid-period rate changes

- `TariffBillingEngine` prices a whole bill from one version, chosen by `service_date`. It **does not split usage** across a mid-period change.
- `ProgressNCBillReconciliationService` splits only when the bill itself prints sub-period kWh rows (`_build_split_energy_estimated_items_from_bill_quantities`). Each sub-period is then priced with the version in effect at its end. No usage allocation is invented when the bill does not print the split, and none was added here. The known −$0.45 / −$0.40 straddle on bills 2025-12-18 and 2026-01-21 (summary-rider proration) is unchanged and still frozen in the baseline.

### 4.3 Behaviour differences (intentional)

- `test_future_only_returns_earliest` asserted the defect. It is now `test_future_only_returns_none`.
- Ten existing `test_tariff_engine.py` tests billed 2025 service dates against fixture versions effective 2025-10-01 or 2026-01-01, so they only passed through the future fallback. The four fixture versions are now dated 2024-10-01. The same versions are selected, and every asserted amount is unchanged.
- `tariff_completeness_audit` also uses `_select_version`, so it now reports future-only families as missing instead of complete.
- Live-DB differences: see §7.3.

## 5. Unsupported charge types

`duke-rates provenance unsupported-charges` classified all 260 unpriced production charges (read-only). The full list is in `reports/phase2a/unsupported_charges.json`.

| Category | Rows | Types |
|---|---:|---|
| Intentionally excluded / informational | 177 | `adjustment_total` 145 (rider-summary TOTAL checksum rows; pricing them would double-count), `incentive` 16, `rebate` 13, `Program Incentive` 3 (customer program payments) |
| Supported but incorrectly mapped | 75 | `Rider Adjustment` 22 → adjustment (includes leaf-607 Storm Recovery and leaf-605); `energy` 21 → tou_energy or energy_block (leaf-525/670, RSC, NMB, leaf-669, leaf-524, leaf-504 discount); `Energy Charge` 12, `Fixed Monthly Charge` 12 (CEPS, SCG, leaf-573/654/745), `rider_adjustment` 3, `Demand Charge` 3, `Basic Facilities Charge` 1, `Seasonal Rate` 1 |
| Billable but unsupported | 5 | leaf-504 R-TOU-EV `standard` period (2); leaf-534 RTP adder (1); percentage `Rider Adjustment` on leaf-590 and leaf-654 (2) |
| Ambiguous / invalid | 3 | `Fee` (2: leaf-573, leaf-800); BPM true-up "Total Adjustment" stored beside its components (1) |

The engine change is reporting only:

- **Base charges:** any applicable charge in the last three categories goes into `BillResult.unsupported_charges` (source, id, type, label, value, unit, category, suggested type, reason) and one warning per source. It is never added to the bill.
- **Rider charges:** same, per rider.
- **Intentionally excluded types:** no warning, so `adjustment_total` keeps its checksum-only role.
- **Unit warning (new finding):** 35 *priced* `adjustment` rows store `¢/kWh`. These are leaf-607 Storm Recovery, 7 versions 2021-12 to 2025-01. `_rate_in_dollars` converts only units containing "cent", so these rows are priced as **dollars**/kWh, 100× too high. The engine now warns. The calculation itself is **not** changed (see §9).

Suggested types are reported, never applied. Normalising stored types is a data change that needs review.

## 6. Provenance, comparison and promotion: scratch validation

On a full-size scratch copy of production (5.8 GB; `reports/phase2a/scratch_migration.json`):

| Step | Result |
|---|---|
| Migrate | 13 tables + triggers. Schema hash changes; the 8 legacy tables' row hashes do not. |
| Backfill | 1,001 `tariff_version` legacy runs and 5,198 charges in 1.4 s. These include **13 orphan version ids** holding 116 charges whose `tariff_versions` row was deleted in April 2026 (Carolinas lighting schedules and others). They are unreachable by billing, but now tracked. Also 50 `parsed_result_json` and 12 `statement_json` payload runs. DB +8 MB. |
| Verify | `ok`: every live row equals its backfilled copy byte for byte, every payload SHA matches, and no charge is untracked. |
| Provenance completeness (legacy) | page 0%, bbox 0%, quote `legacy_unverified` where a snippet existed, source PDF `legacy_unverified`. Nothing is claimed as verified. |
| End-to-end candidate | §6.1 |
| Migration rollback | §6.2 |

### 6.1 End-to-end candidate extraction

This ran on a second scratch copy, so the live baseline run was not disturbed. Outputs are in `reports/phase2a/e2e/`. Target: hd 29, the DEP leaf-500 RES sheet effective 2024-10-01, version 5299, which prices six of the twelve baseline bills.

1. `provenance extract-candidate --historical-document-id 29`: run 1064, profile `progress_residential_flat`, 5 charges, validation `passed`. `tariff_charges` was unchanged.
2. `provenance compare --run-id 1064`: values, units and dates are all identical. **All 5 rows would lose `customer_class='residential'`** (live rows got it from `backfill_customer_class_from_label.py`; the extractor does not emit it). Provenance improves: the quote becomes `available` and the method `parser_profile:progress_residential_flat`. This is the kind of silent regression that an in-place `process_document` re-run would have made.
3. `provenance promote --expected-active-run-id 496`: 5 rows deleted and 5 inserted (ids 757109–757113), log 1064. `verify` ok.
4. `provenance rollback`: the original rows are back with their original ids (753021–753025) and the same content hash. Log 1065 has `reverts_log_id=1064`. `verify` ok.
5. `tariff_charge_revisions` holds 20 receipts, every one attributed to `provenance.promote:` or `provenance.rollback:phase2a-e2e`.

### 6.2 Migration rollback

On the same copy, after the promote and rollback above:

- `provenance drop-schema` **refused**, because the schema held 1 non-legacy run, 2 promotions and 20 charge revisions.
- A forced drop then restored the schema hash and all 8 legacy tables' row hashes to exactly their pre-migration values (`reports/phase2a/scratch_e2e_migration_rollback.json`).

## 7. Validation

### 7.1 Test suite

| Run | Result |
|---|---|
| Full suite, no live DB, final tree (`reports/phase2a/pytest_full.txt`) | **1545 passed, 91 skipped, 4 xfailed, 0 failed** (Phase 1C: 1474 passed / 92 skipped) |
| First pass, before the CLI snapshot update (`pytest_full_first_pass.txt`) | 1542 passed, 2 failed. The failures were `test_cli_command_surface`: 12 new `provenance` commands. The snapshot and count (337 → 349) were updated deliberately. |
| New tests | 28 in `test_extraction_provenance.py`, 32 in `test_billing_safety.py`, 2 selector tests |

### 7.2 Frozen 12-bill baseline

`DUKE_RATES_TEST_LIVE_DB` was set to the **migrated and backfilled** scratch copy, with provenance triggers active (`reports/phase2a/billing_baseline_live.txt`). Result: **2 passed** (`test_live_reconciliation_reproduces_frozen_baseline`, `test_live_stored_effective_dates_are_readable`) in 46 min. All 12 bills reproduce the frozen version selection and component estimates to ±$0.005. The new historical-selector guard does not fire for any of them: every bill selects a covering or latest-prior version. **No bill calculation changed.**

### 7.3 Live-engine tests

`tests/test_tariff_engine_live.py` ran on a production-content scratch copy with both trees (`tariff_engine_live.txt`, `tariff_engine_live_before_25def84.txt`). Both give 11 failed / 13 passed, the same failing set as Phase 1C §5. Every failure has identical numbers; only the `repr` changed (the two new `BillResult` fields).

The one material difference is hidden behind an assertion that fails both ways: `test_partial_tou_coverage_warns_and_reduces_confidence` bills leaf-504 (R-TOU-EV) for 2025-11-01, but its only version is effective 2026-01-01 (`reports/phase2a/future_version_example_leaf504.txt`):

| Tree | Result for 1,000 kWh (300 on / 700 off) |
|---|---|
| `25def84` | priced from the 2026-01-01 version: **$13,985.84**, no coverage warning |
| Phase 2A | `coverage_status="incomplete"`, $0, "no version of nc-progress-leaf-504 was in effect on 2025-11-01; the earliest version on file is effective 2026-01-01. Not priced." |

### 7.4 Extraction benchmark (26 documents)

Run on the migrated scratch copy with the Phase 2A tree (`reports/phase2a/bench_phase2a.json`, scored in `reports/phase2a/bench_scores.md`). **All 26 documents produce identical output to Phase 0 `repaired`**, ignoring timing. Value accuracy 0.7607, precision 0.9806, unit 1.000, rate-class 1.000, effective-date 0.65, TOTAL reconciliation 0.7179, zero-doc FP 0. Wall time was 28.5 s vs 16.7 s, because the live baseline was running concurrently on the same disk. No extraction code changed in this phase.

## 8. Metadata writers and migration strategy

Every writer is audited from the moment the schema is installed, because the triggers fire regardless of code path. Attribution is what still needs routing:

| Writer | Today | Strategy |
|---|---|---|
| 31 scripts under `scripts/` (list in `reports/phase2a/metadata_writers.txt`) | Direct SQL. Recorded as `unattributed`. | **M5:** set `provenance_write_context` from a shared helper (one line per script), or archive the one-off ones. Then add the SQLite authorizer guard from the plan, so unattributed writes to `tariff_charges` / dates / bindings fail loudly. |
| `src/`: `cli.py` (normalize-nc-effective-dates), `cli_commands/lineage.py`, `cli_commands/export_audit.py`, `db/duplicate_detector.py`, `historical/ncuc/effective_date_remediation.py`, `missing_doc_remediation.py`, `missing_doc_workflow.py`, `Repository` (date, binding and parse-result writers), `BulkExtractor.insert_charges` | Direct SQL. Audited, unattributed. | **M3/M4:** route date and binding edits through `revise_metadata`, re-extraction through `extract_candidate` → `promote`, and `save_historical_parse_result` through a payload run. |
| `scripts/corrections/apply_corrections.py` | Receipts-based. | **M6:** apply reviewed C1–C8 and P-items as `charge_overrides` with `source_ref`. |

## 9. Known limitations

1. **Production is not migrated.** That is deliberate. Migrating it is a separate, approved step: backup, `provenance migrate`, `provenance backfill`, then `provenance verify`.
2. **Writers are not yet routed (M3/M4).** `BulkExtractor.process_document`, `tariff-update --auto-parse` and the scripts still overwrite in place. Once the schema is installed, every such overwrite is recoverable from `tariff_charge_revisions` / `document_payload_revisions` / `metadata_revisions`, but it is not prevented.
3. **No page, bbox or value-bearing quote from the current extractor.** Profiles emit no page or bbox, and their snippet is usually the label (benchmark value-in-snippet rate: 3.4%). Candidate runs therefore store page and bbox as `unavailable`. A snippet counts as a quote only when it prints the value. Real page and bbox capture needs the `DocumentRepresentation` work.
4. **Rider summaries (`rider_summary_blocks` / `rider_line_items`) are not versioned yet.** The leaf-600 path that prices riders in the 12-bill baseline still uses delete and re-insert (`persist_rider_summary_records`). Deferred to 2b, as in the plan.
5. **Overrides match by natural key only.** A label rewording breaks the match. Unmatched overrides are not yet flagged.
6. **`¢/kWh` pricing defect** (§5) is warned about but not fixed. Fixing it changes `TariffBillingEngine` totals for leaf-607 2021–2025, which needs your sign-off.
7. **Unsupported types are not normalised.** The 75 mismapped rows are still invisible to pricing until a reviewed data change maps them. Some also carry `¢` units.
8. **`extract_candidate` reuses `BulkExtractor`'s own connection** for document lookup. Docling artifact caching can still write the cache tables, as it does in the benchmark.
9. **Retention** of non-promoted runs is unbounded (open question in the plan).

## 10. Production integrity

- Production SHA-256 is `fe563a783cc78d6bb1a561685f1d83c81d7ea4c87729472ede96e192e888cd08` both before Phase 2A and after all runs. This is identical to the Phase 1C close.
- Production has 0 provenance tables. Its 8 legacy tables hash identically to the scratch pre-migration snapshot.
- Production was only ever opened read-only (`mode=ro`, `immutable=1`): for charge-type queries, the unsupported-charge report and the snapshot comparison.
- Scratch copies (session scratchpad only, not committed): `phase2a_scratch.db` (migrated + backfilled; baseline and benchmark) and `phase2a_e2e.db` (end-to-end promote/rollback, then schema dropped).

## 11. Recommended next milestone (Phase 2B)

1. Approve and run the production migration (M1+M2): backup, migrate, backfill, verify. It takes about 2 s plus the backup.
2. M3: route `BulkExtractor.process_document` and `Repository.replace_parsed_tariff_data` through candidate runs plus explicit promotion. Route `save_historical_parse_result` and the bill parser import through payload runs.
3. M4: route `normalize-nc-effective-dates`, the lineage commands and the remediation modules through `revise_metadata`.
4. Version `rider_summary_blocks` / `rider_line_items` the same way, since they are on the 12-bill path.
5. Decide on the `¢/kWh` fix and a reviewed charge-type normalisation (75 rows). Then M6: apply C1–C8 and the verified P-items as overrides.
6. Start `DocumentRepresentation` capture (page, bbox, quote) so new candidate runs carry real provenance and model comparisons have a common target.
