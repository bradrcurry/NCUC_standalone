# Phase 2E M1.1: Native-Fallback Extraction Correctness

**Date:** 2026-10-28
**Branch:** `phase2e/m11-fallback-correctness`, cut from `phase2e/m1-structured-input-integrity` @ `373b4e1`. Not pushed or merged.
**Background:** `docs/reports/2026-10-28_phase2e_m1_integrity_implementation.md` §6.3 (the five regressions).
**Environment:** `.venv312`, unchanged. No dependency changes.
**Production DB:** never opened for writing. Scratch copy `m11.db` (session scratchpad) for every integration run. SHA-256 before and after: §8.

---

## 0. Summary

M1 named five documents on which the restored native fallback was worse than the old text parse. All five are now explained from the source pages and fixed in **shared** extraction code. None of the fixes is a document-specific number patch.

| Document(s) | M1 said | Verified root cause | Fix | Before (M1) → after |
|---|---|---|---|---|
| hd 7724, 3687, 5365 (DEP Rider JAA-6) | native finds 3 of 8 rates | **Segmentation of the native table.** A class name that wraps ("Seasonal and Intermittent / Service") split one table into two. The second table has no "Rate Class" header, so the class-rate parser skipped it. The structured result then **replaced** the 8-rate text parse. | Native geometry joins a wrapped first-column label to its value line. The JAA profile keeps text-parsed classes the table read lacks. | 3 → **8** each, all with page evidence |
| hd 7265 (DEP Leaf 600, "Traffic Signal") | 9 Traffic Signal rows read as `$/kW` | **Column/unit assignment in the coordinate parser** (`parse_rider_summary_from_pdf`). Fixed x bands (`col1 < 390pt`) put every cents/kWh value printed at x≈391 in the `$/kW` column. This hits **every** class on this layout, not only Traffic Signal: 81 rows, plus 11 lost TOTALs. It runs in **legacy and production** too, so it is not an M1 regression. | Columns come from the page's own "cents" / "dollars" headers above each section. The fixed bands remain the fallback when no header is printed. | 81 wrong-unit rows → **0**; TOTALs 3 → **14**; 104 → 125 charges |
| hd 536 (DEC Leaf 99, OCR'd) | ~33 extra rows that look like duplicates | **Not duplicates. Lost rate-class context.** Page 9 holds four distinct classes (SGS/BC/SGST, LGS, OPT-G/OPT-E, OPT-H). They share 7 of 8 riders and differ in Cost of Removal and TOTAL. OCR damage in three header rows ("EfTeclivc", "ccnts/k\Vh", "Sen ice") stopped the inline-header detector, so the four classes merged under the first label. | Inline-header detection tolerates unreadable header cells when the row names a rate class and one header cell is readable. An unreadable unit cell takes the role printed for the same column above it in the same table, with a warning. | 4 classes merged → **4 separate classes**, each matching the printed TOTAL |

- **M1 protections are intact.** Contamination probe over the 177 exposed documents:
  - charges with evidence from an unverifiable Docling artifact: **0** (M1: 0);
  - evidence outside the span: **0**;
  - 172 of 177 documents give identical charge sets to M1; the 5 that change are exactly the target documents.
- **Benchmarks:**
  - committed-cache legacy, docling and hybrid runs, and both production-artifact runs, are **field-identical to M1**, including evidence pages;
  - native mode changes on one document (`hd1882_jaa_rev3`, the same JAA wrap), where all 5 newly found rates match the hand-transcribed ground truth: recall 0.6792 → 0.6855, precision 0.9801 → 0.9803.
- **Tests:**
  - 16 new source-grounded tests;
  - 356 related structured-extraction tests pass;
  - full suite: §6.
- **Production finding, not fixed here (read-only):** 615 production `tariff_charges` rows on **7** Leaf-600 versions carry `$/kW` on cents/kWh riders. Only hd 7265's 81 come from code that is still live. The other 534 are stale output from an older code generation: current code already extracts those documents correctly. See §5.

## 1. Repository state

| Item | Value |
|---|---|
| Start | `phase2e/m1-structured-input-integrity` @ `373b4e1` (M1 report commit), working tree with 50 pre-existing untracked files (scripts/`_*.py`, reports/`_*.txt`, `fake.db`, …) |
| Untracked work | Preserved untouched. Nothing was staged from it. |
| Baseline tree | Detached worktree of `373b4e1` in the session scratchpad (`m1_base`), used for every before/after run |
| Scratch DB | `m11.db`, a byte copy of production taken at session start |
| M1 artifacts reused | `reports/phase2e_m1/bench/*`, `contamination_{before,after}.json`, `docling_page_integrity_audit.json` |

## 2. Investigation (source pages, not parser output)

### 2.1 JAA Leaf 602: hd 7724 (p.143–146), hd 3687 (p.154), hd 5365 (p.75–78)

**Source.** The span's last page is the Rider JAA-6 sheet: "Supersedes Schedule JAA-5 / Effective for service rendered on and after December 1, 2021 / NCUC Docket No. E-2, Sub 1274". All three documents bind the same sheet in different bundles.

Printed values (dollars; Non-Demand per kWh, Demand per kW):

| Class | Schedules | Unit | Rate |
|---|---|---|---|
| Residential | RES, R-TOUD, R-TOUE, R-TOU | $/kWh | 0.00482 |
| Small General Service | SGS, SGS-TOUE | $/kWh | 0.00462 |
| Medium General Service | CH-TOUE, CSE, CSG | $/kWh | 0.00408 |
| Seasonal and Intermittent Service | SI | $/kWh | 0.00651 |
| Traffic Signal Service | TSS, TFS | $/kWh | 0.00277 |
| Outdoor Lighting Service | ALS, SLS, SLR, SFLS | $/kWh | 0.00000 |
| Medium General Service (demand) | MGS, GS-TES, AP-TES, SGS-TOU | $/kW | 1.51 |
| Large General Service (demand) | LGS, LGS-TOU | $/kW | 1.57 |

**What each path produced (M1 tree):**

- **Text parse (legacy and production):** all 8.
- **Pre-M1 hybrid:** the chunk-relative Docling slice was empty, the structured JAA parse found nothing, and the text parse stood (8).
- **M1 hybrid (native fallback):** 3. They were Residential, SGS and MGS ($/kWh), all from table 1.

**Geometry.** The SI row prints its name on two lines. Line 1 is "Seasonal and Intermittent" + "SI" (schedule column); line 2 is "Service" + 0.00651. Line spacing inside the row is 1.7pt, against 6pt between rows.

`_geometry_tables` ends a table at a value-less line unless that line is a single short label or a header. This line has two cells, so the table ended. Lines 2 onwards became a second table: `Service | 0.00651`, Traffic Signal, Outdoor Lighting, the Demand section, MGS, LGS. It has no "Rate Class" header in its first three rows, so `parse_class_rate_tables` skipped it. The row cells and unit headers were present; the table boundary was wrong.

**Second defect: replace instead of merge.** `ProgressJaaRiderProfile.extract` returned the structured rows whenever there was at least one. A partial table read therefore silently discarded classes the text parse had found.

**Ruled out:**
- Missing cells: every value is in the native word stream.
- Unit interpretation: the section headers give the units.
- Continuation across pages: the sheet is one page.
- Rate-class association: correct for every row that was read.

### 2.2 Traffic Signal, hd 7265 (DEP First Revised Leaf No. 600)

**Source.** The PDF is the 102-page E-2 Sub 1300 Sept-2023 bundle. hd 7265's bound span is **page 1, the Index of Tariffs** (see §5.2). Leaf 600 is on PDF pp.96–101, "Effective for service rendered on and after October 1, 2024, NCUC Docket No. E-2, Sub 1300, Order dated August 18, 2023".

Traffic Signal Schedules (p.101) prints **one value column, "cents /kWh"**:

| Rider | ¢/kWh |
|---|---:|
| BA: Fuel / EMF / DSM / EE / Net | 0.000 / 0.450 / 0.042 / 0.424 / 0.916 |
| EDIT-4 | −0.191 |
| JAA | 0.333 |
| CPRE | 0.013 |
| ESM / PIM | 0.000 / 0.000 |
| **TOTAL** | **1.071** |

So 0.450 ¢/kWh = 0.0045 $/kWh, never 0.45 $/kW.

**Where the unit went wrong.** I traced each parser separately on the PDF:

1. `parse_rider_summary_from_pdf` (coordinate parser, PyMuPDF spans) buckets numbers by fixed x bands: `col1` (cents) if x0 < 390, `col2` (dollars/kW) if x0 < 450.
   - On this layout, values start at x0 = 391.2 (right edge 413.7), under a "cents" header centred at x≈396.
   - Every positive value therefore landed in `col2` = `$/kW`.
   - Negative values ("−0.191") start at 388.2 and stayed cents, which is why EDIT-4 alone was right.
   - The TOTAL rows take `col1`, so they were lost (only 3 of 14 TOTALs survived).
2. The profile then compares TOTAL reconciliation of the coordinate and text parses. The text it receives is the bound span, page 1 (the index), which has no summary. The text parse scores (0, 0); the coordinate parse scores (1, 2) and wins.
3. M1 itself changed one thing only. The wrong-page Docling tables had previously overridden the Traffic Signal block in hybrid mode, so M1 saw a 9-row diff. Legacy mode and production have the same 81 wrong rows.

**Not the cause:**
- Native table detection: the native rep of page 1 has no summary.
- Charge-type classification.
- Unit normalization: canonical units are applied correctly to whatever column the value is in.

### 2.3 Duplicate-looking charges, hd 536 (DEC Amended Fifteenth Revised Leaf No. 99, OCR)

**Source, page 9.** Four rate-class sections:

| Section (as printed / as OCR'd) | Cost of Removal ¢/kWh | TOTAL ¢/kWh |
|---|---:|---:|
| General Service Schedules SGS, BC, SGST | −0.0694 | 0.0483 |
| General Service Schedule LGS ("Sen ice") | −0.0492 | 0.0685 |
| General Service Schedules OPT-G, OPT-E | −0.0390 | 0.0787 |
| General Service Schedule OPT-H ("Serv ice") | −0.0295 | 0.0882 |

The other seven riders are identical across the four (FCA −0.1021, EE 0.2163, DSM −0.0029, Coal 0.0305, MCM −0.0190, BPM-P −0.0733, BPM-T 0.0682).

**What M1's 33 extra rows were.** Native geometry reads page 9 as one 40-row table with inline header rows. Each header row was checked by `_is_inline_header`, which requires every non-label cell to classify as a value or date header:
- LGS: "cents/kWh | **EfTeclivc** | Date". "EfTeclivc" is OCR for "Effective".
- OPT-G and OPT-H: "**ccnts/k\Vh** | Effective | Date".

None qualified, so all four sections ran into the first block, "General Service Schedules SGS. BC. SGST": 8 riders × 4, with four different Cost of Removal values and the last section's TOTAL.

**Verdict.** These rows are **genuine distinct charges with the wrong rate class**, not duplicates, not repeated headers, not redline values (the page has no redline marks), and not overlapping windows. Removing them by value would have deleted three real rate classes.

**Separate, pre-existing identity collision (documented, not changed).** After the fix, 10 rows remain whose (type, label, value, unit) keys match another row's. All come from the **legacy text side** of the hybrid merge (no page evidence):
- "Residential Schedule RT" and "Residential Schedule WT" both become "Residential Schedule" in `_rate_class`, which moves the schedule codes into `applicable_schedules`;
- the same happens for two IIP variants.

The blocks are distinct (their Cost of Removal values differ). Production has the same collision (69 rows labelled "General Service Schedule"). Fixing it changes charge labels the bench, gold and production rows key on, so it is listed under §7 rather than changed here.

## 3. Code changes

| File | Symbol | Change | GitNexus upstream risk |
|---|---|---|---|
| `document_intelligence/adapters.py` | new `_is_wrapped_row_label`; `_merge_wrapped_labels` | Joins a value-less multi-cell line to the next line when all of these hold: same left x (±3pt); gap < 4pt (rows here are ~6pt apart); the next line carries values; the two lines' non-label cells occupy different columns; the first line's cells sit left of the first value. | LOW (1 direct caller) |
| `historical/ncuc/pipeline/parser_profiles.py` | `ProgressJaaRiderProfile.extract`, new `_text_charges` | Structured rows first. Text-parsed classes missing from the table read are kept (no evidence, confidence 0.90, as before). The text parse logic moved into a helper, unchanged. | LOW |
| `parse/rider_summary.py` | new `_column_header_groups`, `_column_from_headers`; `_extract_span_rows` | "cents" / "dollars" header words right of the label area are grouped per section (within 25pt vertically). Each value takes the column of the nearest header centre in the closest group above it (≤ 40pt). Roles keep the parser's existing meaning: first cents → col1, second (incremental) cents → col2, dollars → col2, or col3 after two cents columns. With no header above, the fixed x bands apply exactly as before. | LOW (1 direct caller) |
| `parse/rider_summary_tables.py` | `_is_inline_header`, `_header_columns`, call site in `parse_rider_summary_from_representation` | The original all-cells-are-headers test is unchanged and comes first. Added: a row whose label looks like a rate class, has at least one readable value or date header cell, and has no digits in its other cells is a header. In `_header_columns`, a cell that classifies as "other" takes the role, unit and qualifier of the same column index in the columns in effect, which came from the header printed above it in the same table. The substitution is added to the block's warnings. | **HIGH** (see below) |

**GitNexus (index 7 commits behind; none of these symbols was touched by those commits).**
- `_is_inline_header` and `_header_columns` are rated **HIGH**: 1 direct caller, but on the `parse_rider_summary_from_representation` flow, which handles every structured rider-summary sheet. I flagged this before editing.
- Mitigation:
  - the edit is additive;
  - the committed-cache benchmark, which exercises Docling and native rider summaries for DEP and DEC, is field-identical in hybrid and docling mode;
  - the 356 related tests pass.
- `detect_changes` output is in §6.

**What was deliberately not changed:**
- `page_integrity`, `_structured_representation` routing, the evidence guard, candidate-only persistence, promotion gates, and the unit-normalization tables.
- No value is assumed: an unreadable header borrows a unit only from the same physical column of the same table, and says so in a warning.

## 4. Regression fixtures and tests

Fixtures are the source pages, cut unchanged with PyMuPDF (`tests/fixtures/m11/`):

| Fixture | Source | Pages | SHA-256 (fixture) |
|---|---|---|---|
| `dep_jaa6_e7sub1243_p146.pdf` (109 KB) | hd 7724 PDF `4e0c2c78…` | 146 | `b31a48d1…6edffd8d` |
| `dep_leaf600_e2sub1300_p96-101.pdf` (176 KB) | hd 7265 PDF `7b9171ff…` | 96–101 | `5a747f71…1bfacbe70` |
| `dec_leaf99_e7sub1026_amended_p9.pdf` (34 KB) | hd 536 PDF | 9 | `dcddc07a…a74066f49` |

`tests/test_m11_fallback_recovery.py`, 16 tests:

| Measure | Test |
|---|---|
| Exact charge recall, values, units (JAA, 8 rates) | `test_jaa_profile_recovers_all_eight_rates_from_native_structure`, `test_native_jaa_table_is_one_table_with_every_class` |
| Partial table must not drop classes | `test_jaa_structured_read_never_drops_classes_the_text_parse_finds` |
| Wrapped-label join and its negatives (normal spacing; same column occupied) | `test_wrapped_class_name_joins_its_value_line`, `test_separate_rows_are_not_joined` |
| Unit accuracy, Traffic Signal (all 10 riders in ¢/kWh, no $/kW) | `test_coordinate_parser_takes_columns_from_the_printed_headers` |
| Demand ($/kW JAA) and HP baseline/incremental columns still right | `test_coordinate_parser_keeps_demand_and_incremental_columns` |
| Published TOTAL reconciliation (11/11 blocks) | `test_every_leaf600_block_total_reconciles` |
| Profile output units | `test_leaf600_profile_emits_energy_units_for_energy_only_classes` |
| Duplicate vs distinct (4 classes, Cost of Removal and TOTAL each), rate-class accuracy | `test_ocr_damaged_inline_headers_open_their_own_rate_classes` |
| Header tolerance and its negatives (digits, non-class label) | `test_inline_header_tolerates_unreadable_header_cells` |
| Source-page provenance (evidence page inside span) | page assertions in the JAA, Leaf 99 and archived tests |
| All five archived documents at their real spans (skipped when `data/` is absent) | `test_archived_jaa_documents_recover_all_rates[hd7724,hd3687,hd5365]`, `test_archived_leaf600_bundle_hd7265`, `test_archived_leaf99_hd536_page9` |

**The tests detect the defects.** Run inside the `373b4e1` worktree: **10 fail**, 5 skip (no `data/` there), and 1 passes (the negative case for row joining).

## 5. Before / after

### 5.1 Contamination probe (177 M1-exposed documents, hybrid, extract-only)

`reports/phase2e_m11/contamination_after_m11.json` vs `reports/phase2e_m1/contamination_after.json`:

| | M1 | M1.1 |
|---|---:|---:|
| Documents (errors) | 177 (0) | 177 (0) |
| Charges | 1,756 | 1,804 |
| Charges with evidence from an unverifiable Docling artifact | **0** | **0** |
| Evidence outside the document's span | 0 | 0 |
| Identical charge sets | — | 172 / 177 |

| hd | Pre-M1 | M1 | M1.1 | Charges with evidence |
|---|---:|---:|---:|---|
| 7724 | 8 | 3 | **8** | 3 → 8 |
| 3687 | 8 | 3 | **8** | 3 → 8 |
| 5365 | 8 | 3 | **8** | 3 → 8 |
| 7265 | 105 | 104 | **125** | 0 → 0 (coordinate parser; no structured evidence) |
| 536 | 78 | 111 | **123** | 81 → 101 |

hd 536's +57 / −45 is the relabelling of the four page-9 classes (§2.3) plus distinct rows on pp.10–11 that the same header fix separated.

### 5.2 Leaf 600 exposure (read-only)

Production `tariff_charges` with `$/kW` on non-JAA, non-TOTAL Leaf-600 rows:

| Version | hd | Effective | Production rows `$/kW` | Current code (M1 tree) | M1.1 |
|---|---|---|---:|---:|---:|
| 6908 | 7265 | (null) | 81 | 81 | **0** |
| 6056 | 2275 | 2023-10-01 | 91 | 0 (extracts nothing: span is the index page) | 0 |
| 7265 | 7679 | 2023-12-01 | 110 | 0 | 0 |
| 6548 | 5313 | 2024-01-24 | 70 | 0 | 0 |
| 6927 | 7557 | 2025-01-05 | 84 | 0 | 0 |
| 6057 | 2498 | 2025-01-16 | 85 | 0 | 0 |
| 5357 | 40 | 2025-04-01 | 85 | 0 | 0 |
| 7311 | 7817 | 2026-06-01 | 9 | 0 | 0 |

- **615** suspect production rows. **534** of them are stale: current code already reads those documents correctly. Correcting production is a re-extraction plus promotion decision and was **not** done here.
- Billing reads `rider_line_items` (all-in path), not these `tariff_charges`. That is why the 12-bill baseline never caught this.
- hd 2275 and hd 7265 are both bound to page 1 (the Index of Tariffs) of their bundles. That is a span-binding defect, carried into the M2 assessment (§5 there).

**Coordinate-parser sweep over all 15 Leaf-600 PDFs in the archive** (old vs new, TOTAL reconciliation per block):

| | M1 | M1.1 |
|---|---:|---:|
| Blocks whose TOTAL reconciles | 2 | **45** |
| Blocks whose TOTAL mismatches | 101 | 93 |
| PDFs worse on either count | — | **0** |

The remaining mismatches are on layouts the text parse already wins, and are unchanged.

### 5.3 26-document benchmark

`reports/phase2e_m11/bench/`; `bench/phase2d/diff_runs.py --evidence` against `reports/phase2e_m1/bench/`:

| Run | Verdict vs M1 |
|---|---|
| committed cache: legacy / docling / hybrid | IDENTICAL / IDENTICAL / IDENTICAL |
| committed cache: native | 1 document: `hd1882_jaa_rev3` 3 → 8 charges (the same JAA wrap) |
| production artifacts (`--docling-source db`): hybrid / docling | IDENTICAL / IDENTICAL |

Scores (`reports/phase2e_m11/bench/scores.md`):

| Metric | native M1 | native M1.1 | hybrid (cache) | hybrid (db) |
|---|---:|---:|---:|---:|
| Recall | 0.6792 | **0.6855** | 0.7732 = | 0.7719 = |
| Precision | 0.9801 | 0.9803 | 0.9809 = | 0.9809 = |
| Unit accuracy | 1.0 | 1.0 | 1.0 = | 1.0 = |
| TOTAL reconciliation | 0.6154 | 0.6154 | 0.7179 = | 0.7179 = |
| Zero-doc false positives | 0 | 0 | 0 = | 0 = |
| Page provenance | 0.9892 | 0.9892 | 0.9444 = | 0.8694 = |

## 6. Test outcomes

| Suite | Result |
|---|---|
| `tests/test_m11_fallback_recovery.py` | 16 passed |
| Structured-extraction set (17 files, incl. page-integrity, bounded, candidate pipeline, rider summary parser/segmentation/versioning, profiles, native tables) | 356 passed |
| Full suite (`pytest -q`) | **1,736 passed, 92 skipped, 4 xfailed, 0 failed** in 602 s (`reports/phase2e_m11/pytest_full.txt`). M1 recorded 1,718 passed with 2 snapshot failures; the difference is the 16 new tests plus the 2 snapshot tests M1 fixed. |
| Frozen 12-bill baseline | in the full suite. The billing engine imports none of the changed parsers, and no DB content changed, so the separate `billing_check.py` gate was not re-run. |

## 7. Remaining limitations

1. **Stale production rows (615 on 7 Leaf-600 versions)** stay as they are. A candidate re-extraction of those versions would show 0 suspect rows. Promoting it is a separate, approved step.
2. **Charge identity drops schedule codes** (`_rate_class` / `_extract_block_header`). "Residential Schedule RT" and "… WT" share a label. The structured path keeps them apart as blocks, but labels collide. Fixing this needs a label convention change across bench gold and production keys.
3. **hd 2275 / hd 7265 spans point at the index page.** The coordinate parser reads the whole PDF regardless of span, which hides this for hd 7265. hd 2275 extracts nothing.
4. **The wrapped-label join is narrow on purpose:** two lines, multi-cell first line, <4pt gap. A label wrapping over three lines, or one without a schedule cell, still follows the old rules. No such case exists in the benchmark or the 177 exposed documents.
5. **The OCR header tolerance borrows only within one table.** A section whose first header in a table is unreadable still has no unit (values flagged unit-ambiguous, as before).
6. The 1b72e8de, f76e6a9b, 176ad684 and f5761884 Leaf-600 PDFs still have mismatched TOTALs in the coordinate parse (unchanged by M1.1). The text parse is chosen for them by the existing checksum arbitration.

## 8. Production integrity

| Check | Result |
|---|---|
| SHA-256 before | `fe563a783cc78d6bb1a561685f1d83c81d7ea4c87729472ede96e192e888cd08`, mtime 2026-10-26 12:36:11 (`reports/phase2e_m11/production_sha_before.txt`) |
| SHA-256 after | `fe563a783cc78d6bb1a561685f1d83c81d7ea4c87729472ede96e192e888cd08`, mtime unchanged, `-wal` 0 bytes: **identical** (`reports/phase2e_m11/production_sha_after.txt`) |
| GitNexus `detect_changes` (all) | risk **medium**; 4 files; changed symbols are exactly those in §3, plus line-shift touches (`_geometry_tables`, `_build_blocks_from_rows`, `_total`); affected flows: the three `parse_rider_summary_from_representation` processes |
| Migrations | none run on production; every DB-touching run used `m11.db` or test temp DBs |
| Pushed / merged | no |
