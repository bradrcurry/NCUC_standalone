# Phase 2C: Unified Extraction Pipeline and Safe Bulk Processing

**Date:** 2026-10-27
**Branch:** `phase2c/unified-extraction`, cut from `phase2b/structured-extraction` at `438c7f8`. Local only; not pushed or merged.
**Method:** Production (`data/db/duke_rates.db`) was only hashed and copied. Migration, backfill, extraction runs, promotions, rollbacks and the live billing test ran on one scratch copy (`p2c.db`). No new PDF engine or model was added.

---

## 1. Summary

| Definition of done | Status |
|---|---|
| Bulk extraction produces immutable candidates by default | **Done.** `BulkExtractor.process_document` / `run_extraction` store one sealed `extraction_runs` candidate per attempt through the Phase 2A tables. Nothing is promoted (§3). |
| No normal reprocessing path silently deletes active tariff charges | **Done.** Every caller that went through `process_document` now writes candidates only: `extract-rates-nc`, `mine-tariff-sheets-nc`, `doc-intel mine-docling`, `reprocess process-queue-nc`, `workflow run-nc-missing-doc` and the ingestion scripts. Replacing charges needs an explicit `--legacy-replace` (or `DUKE_RATES_LEGACY_REPLACE=1`). Even then it goes through `provenance.promote`, so it is logged and reversible (§3, §8). |
| Rider-summary history is protected | **Done.** Rider summaries are versioned runs (schema v2). The loader's `replace=True` no longer deletes line items, and out-of-band updates and deletes are audited (§6). |
| Hybrid parsing conflicts are detected and reviewable | **Done.** The Phase 2B extra false positive is traced and fixed. Six conflict validators were added, review findings block promotion, and new CLI commands show them (§5, §8). |
| Large filings can be processed without exhausting memory | **Partly verified.** Bounded span conversion and bounded native parsing are implemented and tested. Native was measured on hd 5324. The Docling measurement could not run on this machine today (§7). |
| Candidates can be compared, promoted and rolled back | **Done.** For charges and for rider summaries, end to end on the scratch copy (§9). |
| Historical billing reconciliation unchanged | **See §9.3.** |
| No unacceptable benchmark regressions | **Done.** Legacy, Docling and native output is identical to Phase 2B field by field. Hybrid loses one false positive and nothing else (§9.2). |
| Production untouched | **Done** (§10). |

## 2. Commits

| Commit | Content |
|---|---|
| `fd3639c` | Hybrid merge: table-only rows need the printed TOTAL to vouch for them. Six conflict validators. |
| `1bfa461` | `BulkExtractor` extract/persist split, candidate default, legacy replace through promote, review gate, rider-summary versioning (schema v2), CLI. |
| `950a47b` | Bounded span conversion, bounded native adapter, memory benchmark. |
| `dddba15` | A one-sided parser reading is `info`, not `review`. Rider-summary e2e script. |

## 3. BulkExtractor integration

Extraction and persistence are now separate steps.

| Method | Writes |
|---|---|
| `extract_document(doc, structured_mode=, version_id=)` → `DocumentExtraction` | Nothing to `tariff_charges` or the run tables. The classification recorders inside `extract_charges_from_document` still run, as before, unless disabled. |
| `persist_candidate(extraction, created_by=)` | One sealed run via `candidate_runs.record_candidate_run`. This is the same code path `provenance extract-candidate` uses, so there is no second persistence implementation. |
| `process_document_result(doc, force_clear=False, legacy_replace=None, created_by=)` → `ProcessResult` | Bookkeeping logs (fingerprint, parse attempt, review outcome, processing run, unchanged), then a candidate run, then a promotion only on a legacy replace. |
| `process_document(...)` | Compatibility wrapper. Returns the same 5-tuple `(doc_id, family_key, num_inserted, status, profile)`. `num_inserted` is the number of rows written to `tariff_charges`, so it is 0 for a candidate run. |

Every run records:
- a unique run id and UUID;
- the source path and SHA-256 (also in `document_blobs`);
- the parser name and git version, and a code fingerprint;
- a config with the pipeline stage version, page span, structured mode, persistence (`candidate` | `legacy_replace`) and bounded-conversion metadata;
- the text source.

Charges keep the Phase 2B evidence: page, bbox, quote, table cell, printed value and unit, and line effective date.

**Legacy replace** promotes the run it just recorded with `provenance.promote`:
- The prior rows are logged, and `provenance rollback` restores them with their ids.
- Rows with notes, LLM-promoted rows and active overrides are kept. This is the same protection `insert_charges` gave.
- A run that failed validation is refused. `insert_charges` wrote anything.
- An empty run is promoted only with `force_clear`, matching the old behaviour.
- A loud warning is logged for every promotion.

**Schema required.** Bulk extraction refuses a database without the provenance schema. It points to `provenance migrate` rather than migrating silently. Production has no provenance schema, so the extraction commands will refuse to run on it until it is migrated. That is intentional (§11).

`insert_charges` is no longer called by any pipeline path. It is kept as a documented low-level writer.

## 4. Extraction modes

| Mode | Meaning |
|---|---|
| `legacy` | Text-only profiles. Reproduces Phase 0/2B output byte for byte (§9.2). |
| `docling` | Cached Docling structure only. |
| `native` | pdfplumber text layer with geometry tables. |
| `hybrid` | Docling when cached, else native, merged with the text parse section by section. |

- **Candidates** default to `hybrid` (`BulkExtractor.candidate_mode`).
- **Legacy replace** defaults to `legacy`, so promoting without review never switches the active billing interpretation to hybrid.
- An explicitly chosen mode always wins: `--mode`, `structured_mode=`, or `DUKE_RATES_STRUCTURED_MODE`.
- Direct callers of `extract_charges_from_document` keep `legacy` unless they choose a mode (unchanged).
- The mode is stored in each run's `config_json` and shown by `provenance candidates`.

## 5. Hybrid conflicts

### 5.1 The Phase 2B extra false positive

The Phase 2B extra false positive is blk13 (E-2 Sub 1300 redline), **Demand: Medium General Service, PIM = 0.0002 ¢/kWh**. The source prints `0.002 4/1/25`.

- Docling reads the redline as `0.0002`.
- The text parse has no PIM row in that section.
- The page-3 continuation table has fewer than 3 values in the column, so the per-column precision check could not decide.
- `merge_with_legacy` added the table-only row because the block still reconciled. But the block already reconciled within the 0.002 ¢ tolerance without the row (residual 0.0003 ¢), so the TOTAL could not vouch for a 0.0002 ¢ value.

**Fix (`_merge_items`).** A table-only row is added only when the printed TOTAL reconciles *because of it*, or reconciles exactly with it.
- Rows printed as exactly zero (the ten ESM `0.000` rows hybrid recovers on blk13) are still added to an already reconciled block: they leave every sum unchanged.
- Other rows are kept out, recorded in `evidence.unvouched_table_rows`, and flagged "needs review".
- Blocks without a TOTAL no longer accept table-only rows.

**Effect on hybrid:** recall stays at 0.7732, false positives go 13 → 12, and precision goes 0.9794 → 0.9809 (legacy: 0.9806).

### 5.2 Validators

All are in `document_intelligence/charge_validators.py`; run on every candidate.

| Check | Code | Level |
|---|---|---|
| Duplicate charges | `duplicate_charge` / `conflicting_duplicates` (Phase 2B) | warning / review |
| Redlined superseded values | `redline_superseded_value`: on a redline copy (classification, ingest flag or struck/inserted header text), a quote printing more numbers than the row has value cells | review |
| Fused redline digits, document-wide | `printed_precision_outlier`: more decimals than ≥ 75 % of values in the same printed unit (needs ≥ 5 values) | review |
| Rate-class conflicts | `rate_class_conflict`, cross-class contamination (Phase 2B) | review |
| Unit conflicts | `type_unit_conflict`, unknown units (Phase 2B) | review / warning |
| Source-page disagreements | `source_page_disagreement` (one charge read from two pages with different values), `page_outside_span` | review |
| Conflicting parser interpretations | `parser_disagreement` (text and table disagree; the text value is kept), `parser_conflict` (the profile's "needs review" warnings, e.g. unvouched rows) | review |
| One-sided reading | `parser_value_missing` (one parse found the row but no value) | info |
| Degraded structured input | `degraded_structured_input` (bounded conversion fell back on some pages) | review |

**Review gate.** `provenance.promote` refuses a run that has review findings unless the caller passes `acknowledge_review` (`--acknowledge-review`). Nothing chooses whichever parser returns more charges: when the readings disagree, the text value is kept and both readings are recorded.

## 6. Versioned rider summaries (provenance schema v2)

**New tables.**
- `rider_summary_sources`: one row per `(docket_dir, source_pdf)`; this is the run target `rider_summary_source`.
- `extracted_rider_blocks` / `extracted_rider_line_items`: immutable once the run is sealed (no update or delete; no insert into a sealed run). They keep links to the run, the source and their block, plus the item page, quote and evidence when the parser had them.
- `materialized_rider_blocks`: links each live block to the extracted block it came from.
- `rider_summary_revisions`: triggers on `rider_summary_blocks` / `rider_line_items` keep the old row on every update or delete, whoever makes it.

**Shared run API.** `seal_run`, `compare_run`, `promote`, `rollback` and `verify_materialization` dispatch on the new target kind.
- Compare matches blocks by rate class and effective date, and items by rider code or label. It reports value, TOTAL, block and item changes.
- Promotion replaces the source's live blocks and logs everything it replaced.
- Rollback restores the blocks and items with their original ids, so live block ids and the residential timeline join are unchanged after a rollback.

**Upgrading a v1 install.** SQLite cannot alter the `extraction_runs` target CHECK. `ensure_provenance_schema` therefore rebuilds that table into the current definition:
- rows and ids are copied and their counts checked;
- the work runs in one transaction with `legacy_alter_table` set, so other tables' references stay valid;
- triggers and indexes are recreated.

Tested from a v1 schema: runs and seal and append-only triggers survive.

**Loader (`ncuc_loader.persist_rider_summary_records`).**
- `replace=False` (insert-only) is unchanged.
- `replace=True` records one candidate run per source PDF and leaves the read model alone. It promotes only on a legacy replace, and it refuses when the schema is absent.
- `load-ncuc-ingest --replace` and `ingest-ncuc --replace` therefore stop overwriting rider blocks.

The read tables keep their columns, so `canonical_rider_components`, the DEP/DEC timelines and the analytics queries run unchanged.

## 7. Large documents

**Cause.** The extractor never converts PDFs; it reads cached Docling only. The Phase 2B failure was the whole-document Docling conversion of hd 5324 (110 pages; tariff span 17–31) in `build_docling_cache`. Separately, `docling_backend._merge_chunk_results` keeps only the *last* chunk's Docling document when it falls back to chunking, so earlier tables and page numbers are lost. That path is unchanged (GitNexus CRITICAL). The new code does not use it.

**`document_intelligence/bounded.py`.** It converts only the document's span, N pages at a time (default 8):
- Each chunk is cut with PyMuPDF; no page images are rendered.
- Each chunk is converted with the existing backend and rebuilt with `page_offset`, so pages keep their numbers in the original filing.
- Tables stay on their page.
- A chunk Docling cannot convert, out of memory included, falls back to the native text layer. The result records `degraded` and the failed ranges, and the candidate run gets a `degraded_structured_input` review finding.

The extractor uses it only when asked (`bounded_conversion`, `DUKE_RATES_BOUNDED_CONVERT=1`), for docling/hybrid documents without a cached artifact.

**Native adapter.** It now parses only the requested pages and releases each page's layout cache. The output is identical (benchmark §9.2).

**Measurements** (`bench/bounded_memory.py`; one subprocess per scenario; a watchdog aborts a scenario when free system memory falls below a threshold):

| hd 5324, 110 pages | Pages | Tables | Seconds | Peak RSS |
|---|---:|---:|---:|---:|
| native, span 17–31 | 15 | 20 | 1.8 | 98 MB |
| native, all pages | 110 | 262 | 11.4 | 129 MB |
| Docling whole document (Phase 2B) | — | — | — | out of memory |
| Docling bounded, span 17–31, 8-page chunks | — | — | — | not measured: aborted by the watchdog at 0.47 GB free |

The Docling scenarios could not run today. The machine had 0.5–2.3 GB free out of 31.5 GB, most of it held by a running Ollama `llama-server` (about 12.5 GB committed). That process is not part of this pipeline and was left alone. The bounded path is covered by unit tests with a stub converter: span only, chunk sizes, original page numbering, and failure → native fallback → degraded. Rerun when memory is free:

```bash
python bench/bounded_memory.py --pdf data/historical/ncuc/e-2-sub-1207/32b7c724-edef-4a3a-bb45-85058c2ec503.pdf --span 17 31 --out reports/phase2c/bounded_memory.json
```

## 8. CLI

**Changed meaning** (documented in `cli_commands/extraction_options.py` and in each command's help):

| Command | Before | Now |
|---|---|---|
| `extract-rates-nc` | Replaced `tariff_charges` per document | Candidate runs. Prints persistence, mode, run count and review count. New `--mode`, `--legacy-replace`. |
| `mine-tariff-sheets-nc` | Replaced charges after mining | Candidate runs; prints the run per document. `--legacy-replace`. |
| `doc-intel mine-docling` | Replaced charges | Candidate runs. `--legacy-replace`. |
| `reprocess process-queue-nc` | Replaced charges; `--enforce-cleanup` deleted charges on empty runs | Candidate runs. `--enforce-cleanup` takes effect only with `--legacy-replace`. Queue metadata records `candidate_run_id`. |
| `workflow run-nc-missing-doc` | Replaced charges (and unpacked 3 values from a 5-tuple: a latent bug, now fixed) | Candidate runs; `DUKE_RATES_LEGACY_REPLACE=1` for the old effect. |
| `load-ncuc-ingest --replace`, `ingest-ncuc --replace` | Overwrote rider blocks and deleted their line items | Rider candidate runs. |
| `scripts/ingestion/*.py` (10 one-off scripts calling `process_document`) | Replaced charges | Candidate runs unless `DUKE_RATES_LEGACY_REPLACE=1`. |

`--legacy-replace` prints a red banner to stderr. Every command prints what it will do to `tariff_charges` before it starts.

**New** `provenance` commands:

| Command | Purpose |
|---|---|
| `extract-candidates` | Bulk candidate extraction: all eligible documents, a family, or listed ids; `--mode`, `--workers`, `--out`. Classification recorders are off, and nothing is promoted. |
| `candidates` | Runs with review state, validation, finding count, mode and persistence. Filters: version, family, kind, state, `--needs-review`. |
| `issues --run-id` | Findings needing review, run-level findings, and per-charge findings with the charge. |
| `evidence --run-id [--charge-id/--label]` | PDF hash, page, bbox, quote, table cell, printed and canonical value, method. |

Also: `extract-candidate --mode`, and `promote --acknowledge-review`.

The CLI surface snapshot is updated: 349 → 353 commands, none removed or renamed.

## 9. Regression results

### 9.1 Tests

| Run | Result |
|---|---|
| New: `test_bulk_candidate_pipeline` (13), `test_rider_summary_versioning` (11), `test_hybrid_conflicts` (14), `test_bounded_extraction` (6) | 44 passed |
| Full suite (`reports/phase2c/pytest_full.txt`) | 1690 passed, 91 skipped, 4 xfailed, 0 failed (12:59) |

Of the existing tests:
- **18 needed no change in assertions.** They check that `process_document` writes `tariff_charges`, so they now run with the `legacy_replace_persistence` fixture (schema installed, legacy replace on). They pass, which shows candidate → validation → promote reproduces the old outcome.
- **Two tests mocked private signatures** and were updated: the reprocess worker fake and the `run_extraction` limit test.

### 9.2 26-document benchmark (`reports/phase2c/bench_scores.md`)

| Metric | legacy | Docling | native | hybrid (2B → 2C) |
|---|---:|---:|---:|---:|
| Recall | 0.7607 | 0.7356 | 0.6792 | 0.7732 → 0.7732 |
| Precision | 0.9806 | 0.9800 | 0.9801 | 0.9794 → **0.9809** |
| Extracted / false positives | 619 / 12 | 599 / 12 | 553 / 11 | 630 / 13 → 629 / 12 |
| Unit accuracy | 1.0 | 1.0 | 1.0 | 1.0 |
| Rate-class accuracy | 1.0 | 1.0 | 0.979 | 1.0 |
| TOTAL reconciliation | 0.7179 | 0.6410 | 0.6154 | 0.7179 |
| Zero-document false positives | 0 | 0 | 0 | 0 |
| Page provenance | 0 | 0.987 | 0.989 | 0.944 |

A field-by-field diff against `reports/phase2b/bench_*.json`:
- **legacy, docling, native:** identical on every document.
- **hybrid:** one change only, the blk13 PIM 0.0002 row removed.

### 9.3 Frozen 12-bill reconciliation

Live 12-bill reconciliation against the migrated scratch copy (`DUKE_RATES_TEST_LIVE_DB=p2c.db`): **2 passed** (34 deselected) in 50:48; see `reports/phase2c/billing_baseline_live.txt`. The frozen baseline is unchanged by Phase 2C.

### 9.4 End to end on the scratch copy (`reports/phase2c/e2e/`)

The scratch copy was migrated to v2 in 2.8 s and backfilled in 4.5 s (1001 versions, 5198 charges, 50 documents, 12 bills). `verify`: ok.

**`provenance extract-candidates`** on hd 2591, 47, 2253 and 5324 (hybrid): 4 sealed runs in 8.7 s, 0 promoted, `tariff_charges` 5198 → 5198. hd 5324 is an expected-zero document (`empty`).

**hd 2591** (leaf-600, 2026-04-01):
1. **First run (1064)** was flagged for review: the table parse found the MGS JAA row without a value. That led to `parser_value_missing` = info (`dddba15`).
2. **Re-run (1068)** passed. Compare against the legacy run 714:
   - 0 value, 0 unit and 0 class-unit changes;
   - 124 rate-class and 76 effective-date differences, from backfilled classes and per-line dates;
   - 1 row removed, the LGS $/kW TOTAL (as in Phase 2B).
3. **`promote --expected-active-run-id 714`:** 125 rows deleted, 124 inserted; verify ok.
4. **`rollback`:** active run 714 again; verify ok; 5198 charges.
5. Both runs remain stored: 1068 `previously_active`, 1064 `candidate`.

**Rider summaries** (`scripts/phase2c/rider_summary_e2e.py`), on the live source `e-2-sub-1300/f76e6a9b…pdf` (26 blocks, 484 items):
1. Recording run 1069 left the read model unchanged.
2. Compare: 11 candidate blocks vs 26 live, 17 removed, 148 value changes.
3. Promote deleted 26 blocks and 484 items and logged 510 revisions; verify ok.
4. Rollback restored all rows with their ids; verify ok.

**Finding.** That source holds three stacked readings of every rate class: 2025-01-01, 2025-04-01 and 8 undated blocks, loaded on 2026-03-19 and 2026-03-31. The current parser reads the redline as 2025-04-01 only. A plain `replace=True` reload would have deleted 17 blocks with no record. The candidate is not promoted; which readings are legitimate is a data decision.

## 10. Production integrity

Production was hashed and copied, then hashed again at the end. It was never opened for writing.

| | SHA-256 |
|---|---|
| Before | `fe563a783cc78d6bb1a561685f1d83c81d7ea4c87729472ede96e192e888cd08` (unchanged since Phase 1C) |
| After | `fe563a783cc78d6bb1a561685f1d83c81d7ea4c87729472ede96e192e888cd08`: identical; file mtime still 2026-10-26 (`reports/phase2c/prod_sha_after.txt`) |

## 11. Known limitations and follow-ups

1. **Production needs `provenance migrate`** before any extraction command can run on it. This is deliberate: bulk extraction refuses rather than migrating silently. Migrating production is a separate, approved step: about 3 s for the migration and 5 s for the backfill on a copy.
2. **Docling bounded conversion is unmeasured** on real hardware (§7). Rerun the benchmark when memory is free.
3. **`docling_backend._merge_chunk_results`** still drops all but the last chunk's document for its own callers (`process-docling-batch`). Bounded conversion avoids it; fixing it in place is CRITICAL-risk and was not done.
4. **Rider-summary data on e-2-sub-1300** (and possibly other redline sources): stacked readings with three effective dates. Review before any rider promotion.
5. **Rider candidates from `ncuc_loader` carry no item evidence.** The serialized text parse has none. Structured parses do record it (`evidence_json`, page, quote).
6. **`promote` with an active run** must name it (`--expected-active-run-id`). This is the Phase 2A concurrency guard; omitting the option means "expect none".
7. **Scripts under `scripts/ingestion/`** were not edited. Several still unpack 3 values from `process_document`, a pre-existing bug. They store candidates unless `DUKE_RATES_LEGACY_REPLACE=1`.
8. **Phase 2B data issues remain open:** the leaf-607 storm duplicates, v5723 contamination and the 0.55 outlier.
9. **The GitNexus index is 15+ commits stale.** New modules are not indexed. Impact analysis for unindexed symbols was done with grep: `_merge_items`, `promote` and the provenance functions have only in-repo callers and tests.

## 12. Recommended next steps

1. Approve and run `provenance migrate` + `backfill` on production, then run `provenance extract-candidates` (hybrid) family by family and review with `candidates --needs-review` / `issues` / `compare`.
2. Rerun the bounded Docling benchmark, then build the hd 5324 span cache with it.
3. Review the stacked rider-summary readings (e-2-sub-1300 first) and record decisions as promotions or rejections.
4. Migrate the next profile group to `table_api` (TOU and tiered schedules, DEC multi-class tables), as planned in Phase 2B.
