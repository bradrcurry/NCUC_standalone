# Phase 2B: Structured Document Extraction and Canonical Charge Normalization

**Date:** 2026-10-27
**Branch:** `phase2b/structured-extraction`, cut from `phase2a/provenance-billing-safety` at `2cee58e`. Local only; not pushed or merged.
**Method:** Production (`data/db/duke_rates.db`) was opened read-only. All extraction runs, candidate runs, promotions and live tests used scratch copies. No new parsing engine was added: Docling output comes from existing cached artifacts or the existing `docling_backend`.

---

## 1. Summary

| Definition of done | Status |
|---|---|
| Docling table structure survives into tariff extraction | **Done.** `representation_from_docling` keeps pages, reading order, tables, cells (with spans and header flags), bounding boxes and the backend version. Tables are no longer flattened before the migrated profiles read them. |
| Selected profiles consume structured data | **Done.** The DEP Leaf 600 and DEC Leaf 99 rider summaries, JAA (Leaf 602) and the Progress single-value riders. |
| Legacy regex fallbacks remain functional | **Done.** `legacy` mode reproduces the Phase 0 `repaired` benchmark output byte for byte. `hybrid` merges each structured parse with the legacy parse section by section. |
| Canonical units and charge types enforced | **Done.** `parse/canonical.py` (Decimal). Unknown or ambiguous units get warnings, never a default. `TariffBillingEngine` now prices `¢/kWh` as cents. |
| Source-page provenance for new candidate charges | **Done.** 94% of hybrid benchmark charges carry a page, a bbox and a quote that prints the value. Every printed value matches its charge (595/595). Candidate runs store page, bbox, quote, table/row/column, printed value and unit, and line effective date. |
| Results comparable and promotable via Phase 2A | **Done.** End-to-end on a scratch copy: candidate, compare, promote, verify, rollback, verify (§6). |
| No unacceptable benchmark regressions | **Done.** Hybrid improves recall by 1.25 points. Unit, rate-class and TOTAL accuracy are unchanged. Precision drops by 0.12 points: one extra false positive, on a redline document (§7). |
| Historical billing baseline valid | **Done.** The live 12-bill reconciliation passes (2 passed, 44 min) on a scratch copy. |
| Production unchanged | **Done.** See §10. |

## 2. Files

| File | Change |
|---|---|
| `src/duke_rates/parse/canonical.py` (new) | Canonical units (`$/month`, `$/kWh`, `$/kW`, `$/kVAR`, per-unit monthly, `$/year`, `$/W`, `$/event`, `$`, `%`, fraction). Charge types: fixed, fixed credit, energy, TOU energy, demand, minimum, energy/demand/fixed/percent adjustment, energy credit, informational TOTAL, incentive, unknown. Explicit aliases, exact Decimal parsing of printed numbers, basis and type conflict warnings. |
| `src/duke_rates/billing/tariff_engine.py` | `_rate_in_dollars` uses the canonical rules, so `¢/kWh` is now cents. A warning for unrecognized units replaces the Phase 2A 100× warning. |
| `src/duke_rates/document_intelligence/models.py` | Additive fields, all with defaults: `TableCell.row_span`, `col_span`, `is_column_header`; `ExtractedTable.table_id`; `DocumentRepresentation.backend_version`, `representation_version`, `coordinates_available`. |
| `src/duke_rates/document_intelligence/adapters.py` (new) | Docling, native (pdfplumber with geometry tables) and OCR-text adapters; canonical JSON serialization and fingerprint; `RepresentationCache` keyed by source SHA-256, backend, backend version and options. |
| `src/duke_rates/document_intelligence/table_api.py` (new) | Shared table helpers: header rows, column roles (label / value + unit + baseline-incremental qualifier / effective date), TOTAL rows, `CellRef` evidence. |
| `src/duke_rates/parse/rider_summary_tables.py` (new) | Structured rider-summary parser that emits the existing `RiderSummaryResult`. Handles page-spanning sections, rate-class titles left outside tables, fused value/date cells and redline digit fusion. `merge_with_legacy` arbitrates per section and per item. |
| `src/duke_rates/parse/class_rate_tables.py` (new) | Parser for "rate by customer class" tables (JAA, per-class single-value riders); the unit comes from section rows or the header. |
| `src/duke_rates/document_intelligence/evidence.py` (new) | Value locator: page, bbox and quote for charges from text-only profiles, attached only when the location is unambiguous. |
| `src/duke_rates/document_intelligence/charge_validators.py` (new) | Validators for TOTAL reconciliation (Decimal), column association, cross-class and cross-family contamination, units, rate-class consistency, effective dates, duplicates and revision changes. |
| `src/duke_rates/parse/rider_summary.py` | Optional `evidence` on `RiderLineItem` and `RiderRateClassBlock`. |
| `src/duke_rates/historical/ncuc/pipeline/rate_extractor.py` | Optional `ExtractedCharge.evidence`. |
| `src/duke_rates/historical/ncuc/pipeline/parser_profiles.py` | Structured-input helpers. The four migrated profiles use them; all other profiles are untouched. |
| `src/duke_rates/historical/ncuc/pipeline/bulk_extractor.py` | `structured_mode` (`legacy` default, `docling`, `native`, `hybrid`; env `DUKE_RATES_STRUCTURED_MODE`), a lazy representation loader for profiles, and the evidence-locator post-pass. `_load_docling_artifact` is unchanged (GitNexus risk CRITICAL). |
| `src/duke_rates/historical/ncuc/pipeline/candidate_runs.py` | Defaults to `hybrid`. Maps evidence into Phase 2A provenance fields and runs the validators into `seal_run`. Structured mode is in the config fingerprint, and the new modules in the code fingerprint. |
| `src/duke_rates/db/provenance.py`, `provenance_schema.py` | `extracted_charges.table_ref_json` (additive, and added by `ensure_provenance_schema` to already-migrated DBs). Canonical value computed exactly from the printed value and unit. Unit added to the natural key. `seal_run(extra_issues=...)`. |
| `bench/build_docling_cache.py` (new), `bench/docling/*.json.gz` (25 docs, 788 KB) | Reproducible Docling cache keyed by corpus SHA-256: 23 from existing `docling_artifacts`, 2 converted with the existing backend. |
| `bench/run_extraction.py`, `bench/score.py` | `--mode`, per-charge evidence summaries, provenance coverage metrics. |
| Tests | `test_canonical_charges.py` (61), `test_structured_extraction.py` (23), `test_structured_profiles.py` (17). `test_billing_safety.py` now expects cents pricing. Two Phase 2A provenance tests were updated: one override test now gives the unit (part of a charge's identity), and one asserts the `:from_printed` method. |

## 3. Storm Recovery (the 35 Phase 2A rows)

The leaf-607 rows for 2021-12-01 to 2025-01-01 store values such as `0.321` with unit `¢/kWh`. The stored snippets read "storm cost recovery charges of 0.321 cents per kWh". **The values are correct cents.** The defect was in the engine: `_rate_in_dollars` divided by 100 only for units containing "cent", so these rows were priced at $0.321/kWh instead of $0.00321/kWh, 100× too high. This is now fixed and tested (`test_storm_recovery_cents_convert_exactly`, `test_cent_sign_and_cents_units_convert_identically`).

The same investigation found data issues. They are not changed here; each is now caught by a validator.

- Version 5718 (2021-12-01) holds both the "existing" (0.300…) and the "new" (0.227…) storm rates as `Rider Adjustment` rows. `validate_duplicates` reports these as conflicting duplicates.
- Version 5723 (leaf-607, 2023-07-01) holds about 60 base-rate charges from other schedules (RES, R-TOU, demand rates). `validate_family_contamination` reports it.
- The 2024-07-01 Lighting value 0.55 sits between 0.042 and 0.149. `validate_revision_changes` reports it.

## 4. Structured representation and table-aware extraction

**Docling.**
- Reading order follows `body` references. Headers and footers are flagged `furniture` (Docling's `content_layer`).
- Bottom-left bboxes are flipped to a top-left origin using the page height. Without the height they are dropped, not guessed.
- Cells keep their spans and column-header flags.
- `page_offset` supports conversions of an excerpt of a PDF.

**Native.** Duke rider summaries have no ruling lines; pdfplumber's line detector finds 0 tables and its text strategy produces garbage. Tables are therefore rebuilt from word geometry:
- words are grouped into lines and split into cells at gaps of 9 pt or more;
- columns are clustered on the right edges of numbers and on unit header words;
- wrapped labels are joined;
- up to three header lines above the first value row are included.

**OCR text.** Pages and paragraphs only. `coordinates_available=False`, and no bboxes are stored.

**Structured rider summary.** Units come from column headers ("cents /kWh", "dollars /kW", "cents /kWh Baseline/Incremental"). Docling artifacts handled:
- split words ("Incrementa l", "Effectiv e");
- values fused with dates ("0.92 12/1/25");
- rate-class titles left outside the table, including at the foot of the previous page;
- continuation tables, mapped to the previous header's columns by x position.

Values printed with more decimals than their column (redline struck+inserted digit fusion, e.g. "0.0002" in a 3-decimal column) are left unset and flagged for review.

**Merge (`hybrid`).** Per section:
- If only one parse reconciles the printed TOTALs, that parse wins.
- Otherwise the sections are merged item by item. Items match by rider code, then label.
  - When both parses agree, the text value is kept and gains the table evidence.
  - When they disagree, the text value is kept, flagged `needs review`, and both readings are recorded.
  - Rows only the table parse found are added only if they are top-level and the section still reconciles. Sub-items are never added, because the TOTAL cannot vouch for them.

**Rider-summary sections reconciled** (printed TOTAL equals component sum), on the 7 summary documents:

| Document | text (legacy) | structured Docling | structured native | hybrid |
|---|---|---|---|---|
| DEP leaf-600 2026-06-01 | 11/11 | 11/11 | 8/11 | 11/11 |
| DEP leaf-600 2026-04-01 | 11/11 | 11/11 | 8/10 | 11/11 |
| DEC leaf-99 2026-06-01 | 6/6 | 6/6 | 4/5 | 6/6 |
| DEC blk307 (printed total is wrong in the source) | 5/6 | 5/6 | 5/6 | 5/6 |
| DEC blk304 (Docling missed 2 tables) | 6/6 | 4/4 | 6/6 | 6/6 |
| DEP blk13 (redline) | 10/11 | 1/11 (fused digits left unset) | 1/11 | 10/11 |
| DEC blk361 | 7/7 | 6/6 | 7/7 | 7/7 |

## 5. Canonical units in billing and extraction

- **Billing:** `_rate_in_dollars` uses `dollars_factor`: cents units × 0.01 (exact Decimal); dollar and percent units unchanged; unrecognized units priced as written, with a warning.
- **Extraction:** candidate charges keep `printed_value_text` and `printed_unit` (the header text as printed, e.g. "cents /kWh"). `canonical_value` is computed from those exactly, and the method is recorded as `…:from_printed`. A disagreement with the profile's float would be recorded as `…:printed_disagrees`; none occurred.
- **Energy vs demand:** the basis comes from the unit. A `demand` charge in `$/kWh`, or an energy charge in `$/kW`, is flagged `type_unit_conflict`. `adjustment_total` rows are canonical `ADJUSTMENT_TOTAL`, which is not billable, so rider-summary totals are never double-counted. Tests cover all three.

## 6. Provenance through Phase 2A (scratch copy `phase2a_e2e.db`)

The copy was migrated and backfilled (`verify` ok). Hybrid candidate runs were then made with `duke-rates provenance extract-candidate`:

| Document | Run | Charges | Page / bbox / quote | Table cell | Validation |
|---|---|---|---|---|---|
| hd 2591 leaf-600 2026-04-01 | 1070 | 124 | 112 / 112 / 112 | 112 | passed (revision info: 1 row of the prior version absent) |
| hd 47 leaf-607 2026-01-01 | 1068 | 5 | 5 / 5 / 5 | 5 | passed |
| hd 2253 JAA 2025-12-01 | 1069 | 8 | 8 / 8 / 8 | 8 | passed |

Example stored row: `BA-EMF 0.00518 $/kWh`, printed `0.518`, printed unit `cents /kWh`, canonical `0.00518` (`identity:from_printed`), page 1, bbox, quote "Fuel and Fuel-Related Adjustment Experience Modification Factor (EMF) 0.518 12/1/25", table `docling:#/tables/0` row 3 column 1, effective_start `2025-12-01`.

`provenance compare --run-id 1070` against the live rows showed 0 value changes and 0 unit changes, plus three kinds of difference:

- **Rate class (124 rows):** the live rows got `customer_class` from a later backfill script.
- **Effective date (69 rows):** the candidate now carries per-line dates.
- **One removed row:** the LGS `$/kW` TOTAL, which the live data has and this run's text parse also misses.

`promote` (125 rows deleted, 124 inserted) → `verify` ok → `rollback` → `verify` ok.

Earlier candidate runs from this session remain stored and immutable (runs 1064–1067), which shows that repeated extractions accumulate rather than replace.

## 7. Benchmark (26 documents; `reports/phase2b/bench_scores.md`)

| Metric | Phase 0 repaired | legacy | Docling | native | hybrid |
|---|---:|---:|---:|---:|---:|
| Value accuracy (recall) | 0.7607 | 0.7607 | 0.7356 | 0.6792 | **0.7732** |
| Precision | 0.9806 | 0.9806 | 0.9800 | 0.9801 | 0.9794 |
| Unit accuracy | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| Rate-class accuracy (summaries) | 1.000 | 1.000 | 1.000 | 0.979 | 1.000 |
| Effective-date accuracy | 0.65 | 0.65 | 0.65 | 0.65 | 0.65 |
| Published TOTAL reconciliation | 0.7179 | 0.7179 | 0.6410 | 0.6154 | 0.7179 |
| Zero-document false positives | 0 | 0 | 0 | 0 | 0 |
| Page provenance | 0 | 0 | 0.987 | 0.989 | 0.944 |
| Bbox provenance | 0 | 0 | 0.987 | 0.989 | 0.944 |
| Table-cell reference | 0 | 0 | 0.880 | 0.873 | 0.840 |
| Quote that prints the value | 0 | 0 | 0.987 | 0.989 | 0.944 |
| Evidence consistent with the value | n/a | n/a | 591/591 | 546/547 | 595/595 |
| Seconds (26 docs) | 16.7 | 18.4 | 20.3 | 39.4 | 20.0 |

**Interpretation, not counts.**

- **`legacy`** produces the same raw output as Phase 0 `repaired` for every document (verified field by field).
- **`hybrid`** differs from legacy on exactly one document: blk13, a redline compliance copy.
  - It finds 10 more correct values: ESM "0.000" rows the text parser drops.
  - It adds one false positive (13 vs 12).
  - On every other document the extracted values, units, classes and totals are identical to legacy. The only change is the added evidence.
- **Pure `docling` and `native`** lose recall where the backend lost structure: Docling missed two tables in blk304; native geometry splits some sections. They are comparison modes, not recommended defaults.
- **Not converted:** hd5324 (110 pages) failed Docling conversion (out of memory). It is an expected-zero document, so the structured modes fall back to legacy for it.

**Effective-date accuracy** is unchanged at 0.65 because the benchmark scores the ingest heuristic's document date. The structured parser now captures *line* effective dates from the date column (90 of 124 rows on hd 2591), which v0 of the benchmark does not score.

## 8. Billing compatibility

- **Frozen 12-bill reconciliation:** `DUKE_RATES_TEST_LIVE_DB` = scratch copy with the Phase 2A schema and backfill. **2 passed**, all 12 bills reproduce (`reports/phase2b/billing_baseline_live.txt`). That path (`ProgressNCHistoricalTariffSelector` and `BillingEngine`) does not use `_rate_in_dollars`.
- **`TariffBillingEngine`:** the only intended calculation change is `¢/kWh` priced as cents. It affects leaf-607 storm rows when that rider is applied, for example $2.46 instead of $246.00 at 1,000 kWh and 0.246 ¢/kWh.
- **Unsupported structures** keep the Phase 2A explicit warnings, plus the unrecognized-unit warning.
- **Full suite:** 1646 passed, 0 failed (§9).

## 9. Test results

| Run | Result |
|---|---|
| Phase 2A + 2B targeted (canonical, structured extraction, structured profiles, provenance, billing safety) | 161 passed |
| Full suite (`reports/phase2b/pytest_full.txt`) | **1646 passed, 91 skipped, 4 xfailed, 0 failed** (Phase 2A: 1545 passed) |
| Live 12-bill baseline (scratch) | 2 passed |

## 10. Production integrity

Production was opened read-only only, for the storm-row query. No extraction, migration or promotion ran against it. SHA-256 `fe563a783cc78d6bb1a561685f1d83c81d7ea4c87729472ede96e192e888cd08` before and after: unchanged since the Phase 1C close.

## 11. Known limitations

1. **The default stays `legacy`** in `BulkExtractor`, so the destructive `process_document` path is unchanged. Only `extract_candidate` (non-destructive) defaults to `hybrid`. Switching the bulk default is a decision for after M3 routes `process_document` through candidate runs.
2. **Only 4 profile families are migrated.** The other 52 profiles get evidence only through the value locator. It refuses ambiguous locations, so repeated values (e.g. the same rider rate in several classes) stay without a page.
3. **Docling coverage.** Most production documents have cached Docling JSON. Those that don't get native geometry in `hybrid`, which is weaker. There is no new conversion during extraction.
4. **Native geometry tables** split some page-spanning sections and are below Docling on reconciliation (§4).
5. **Redline documents.** Fused digits are detected only by column precision. A fusion that keeps the column's precision would not be caught by this check, but it would still fail the TOTAL check unless the error is within 0.002 ¢ (the existing tolerance).
6. **Line effective dates** are captured but not yet scored by the benchmark (v0 limitation).
7. **`parse_effective_date`** (billing path) still reads only ISO and long-form dates. The candidate mapper parses m/d/yy itself.
8. **Bench Docling cache:** 23 of 25 documents come from earlier conversions without a recorded Docling package version (`docling_package_version: null`).
9. **Data issues found** (§3: storm duplicates, contaminated v5723, the 0.55 outlier) are flagged, not corrected. No production records were modified.

## 12. Recommended next milestone (Phase 2C)

1. Route `BulkExtractor.process_document` through candidate runs and explicit promotion (Phase 2A M3). Then make `hybrid` the bulk default.
2. Migrate the next profile group onto `table_api`: TOU and tiered rate schedules (leaf-500 family, DEC RS), and the DEC multi-class rate tables.
3. Extend the benchmark to score line-level effective dates and evidence correctness (page and bbox against hand-labelled locations).
4. Review and correct the leaf-607 data issues through Phase 2A overrides.
5. Version `rider_summary_blocks` / `rider_line_items` with the same run model, now that the structured parser emits per-row evidence.
