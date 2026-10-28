# Phase 2E M1: Structured-Artifact Page Integrity — Implementation Report

**Date:** 2026-10-28
**Branch:** `phase2e/m1-structured-input-integrity`, cut from `phase2e/document-intelligence-reconciliation` @ `219a88a`. Not pushed or merged.
**Plan:** `docs/reports/2026-10-28_phase2e_m1_execution_plan.md`. **Background:** `docs/reports/2026-10-28_architecture_reconciliation.md` §1.2.
**Environment:** `.venv312`, unchanged. No dependency changes.
**Production DB:** opened only with `mode=ro`. SHA-256 before and after: see §11.

---

## 0. Summary

- **Wrong-page structure is blocked.** A new centralized check (`document_intelligence/page_integrity.py`) decides whether a stored Docling artifact can serve a requested range of original-PDF pages. The structured path runs it before building any `DocumentRepresentation`. Unverifiable artifacts never provide tables, blocks, or page provenance.
- **Native fallback is restored.** In hybrid mode, a rejected, partial, or empty Docling slice falls through to bounded conversion (when enabled) or the native PDF read. Before, it suppressed that fallback.
- **Provenance guard.** Evidence citing a page the representation in use does not hold is dropped and reported. Candidate runs record why a Docling artifact was bypassed.
- **Production exposure, read-only:**
  - 107 unverifiable artifacts (all of them `chunk_relative_numbering`, exactly the reconciliation set).
  - 229 bound documents (177 with versions; 180 versions).
  - 1,855 other artifacts validate. None is rejected by mistake.
  - The legacy text slicer would have accepted **none** of the 107, so legacy and production text extraction were never contaminated.
- **Contamination before / after** (hybrid, 177 exposed documents, scratch DB):

  | | Before M1 | After M1 |
  |---|---:|---:|
  | Charges with evidence from an unverifiable artifact | 124 (in 9 documents) | **0** |
  | Charges with evidence (in-span) | 124 | 1,153 |
  | Confirmed wrong-page false positives | 24 (hd 2275) | **0** |

- **Benchmarks:**
  - The committed-cache benchmark is **field-identical to Phase 2C in all four modes**, including evidence pages.
  - With production artifacts, hybrid recall and precision are unchanged. Page provenance rises from 0.8646 to 0.8694.
- **Full suite:** 1,718 passed, 92 skipped, 4 xfailed, 2 failed. Both failures were the CLI command-surface snapshot, which the new command changes on purpose. The snapshot was updated, and that test file now passes (§9).
- **Open issue.** The restored native fallback is worse than the text parse on **5 of the 177** exposed documents (§6.3):
  - a units change on Traffic Signal rows;
  - 15 JAA rates lost on 3 documents;
  - 33 rows that look like duplicates.

  These come from the native path, not from the integrity check. They are candidate-only, but they need fixing before hybrid candidates for these families are promoted.
- **GitNexus risk.** `extract_charges_from_document` is **CRITICAL**. I edited its structured-only reporting block before running impact on it. It was then reduced to a 2-line change that calls a new helper. The legacy branch is untouched, and legacy output is byte-identical (§4.3).

## 1. Root-cause analysis

### 1.1 How chunked results are merged

`convert_pdf_safe` falls back to `_process_all_chunks` / `_resolve_chunk` when a whole-document conversion fails. Each chunk is a temporary PDF cut by `_split_pdf_chunks`, so Docling numbers its pages 1..k. `_merge_chunk_results` then:

| Field | Merged? |
|---|---|
| `plain_text` | Concatenated over all chunks, so complete |
| `tables` (flat list) | Concatenated, without page attribution |
| `page_count` | Summed, so it equals the PDF page count |
| `document` (the structured `doc_json`) | **`chunks[-1]["document"]` only** |
| chunk offset (`page_offset`) | Not carried into the merge, so not stored |

Row metadata is `{"degraded_modes": ["chunked"], "skipped_pages": [], "used_chunking": true}`. `chunked` is not in `DOCLING_QUALITY_LOSS_MODES`, so `_load_docling_artifact` treats the row as full quality.

### 1.2 What survives, and whether the page numbers are original

- Only the last chunk's pages survive, numbered **1..k relative to that chunk**.
- No offset is recorded anywhere, so the original numbers **cannot be recovered** from the artifact.
- The stored `page_count` (the sum) hides the truncation from any check that compares page counts.

### 1.3 Regression fixture: artifact 1661

| Item | Value |
|---|---|
| Source PDF | `data/historical/ncuc/e-2-sub-1300-sept2023/1b72e8de-c435-43b7-9b88-dfc4c0a8a97b.pdf` |
| SHA-256 (row and disk match) | `b61e294063addfaa3e9ad3e156869ad69daffcd441c265613882a7d280621583` |
| PDF pages | 106 (row `page_count` 106) |
| Stored `doc_json` pages | 1–6 (9 tables; row `table_count` 71 is the merged flat list) |
| Real PDF page 1 | *Index of Tariffs — Rate Year 2* |
| Artifact "page 1" | *NC First Revised Original Leaf No. 600* (redline) |
| Where Leaf 600 really is | PDF pages 100–105 mention "Leaf No. 600"; the 6 stored pages best match PDF pages ~100–106 by word overlap. This is approximate on purpose: the exact offset is not stored and is **not inferred**. |
| Bound documents | hd 2275 leaf-600 p.1–1 (tv 6056); hd 7256 leaf-528 p.2–50 (tv 6824); hd 7257 leaf-601 p.52; hd 7258 leaf-532 p.53–56 (tv 6827); hd 7259 leaf-535 p.57–71; hd 7708 leaf-574 p.72–106 |

The committed regression fixture reproduces this shape with:
- a synthetic 106-page PDF whose page 1 starts with the Leaf 600 heading;
- the committed Leaf 600 Docling document (`tests/fixtures/docling/dep_leaf600_2026-04-01.docling.json`) stored as a chunked artifact.

With the guard bypassed (the pre-M1 behavior), hybrid extraction of a leaf-600 span on page 1 yields **23 table-only rider charges, all citing page 1**. With the guard, it yields **0**, and native structure is used.

### 1.4 How structured input selected pages, and why it blocked the fallback

Before M1, `BulkExtractor._structured_representation` worked like this:
1. `_docling_representation(pdf, page_range)` loaded the artifact and called `representation_from_docling(page_range=...)`. That keeps any item whose `prov.page_no` falls in the range, so the chunk-relative pages 1..k were read as original pages.
2. A span overlapping 1..k got **wrong-page structure** under the span's own numbers. That is why the existing `page_outside_span` validator saw nothing: every page number was "in span".
3. A span beyond k got an **empty** `DocumentRepresentation`. It is not `None`, so `if rep is not None ...: return rep` returned it and **skipped the native fallback** in hybrid mode.

The legacy `_slice_docling_text` has its own guard (`max_prov < 0.8 × pdf pages` rejects). The structured path had none.

## 2. Implementation summary

### 2.1 Page-integrity validation (`page_integrity.assess_docling_page_integrity`)

A pure function of:
- the `doc_json`;
- the page count of the PDF on disk;
- the requested range;
- the row metadata;
- the row `page_count`.

It returns a `PageIntegrity` with `status`, `reason`, `numbering`, `covered_pages`, `missing_pages`, `page_offset` and `chunked`.

| Check | Outcome when it fails |
|---|---|
| Source PDF identity | Already enforced upstream: `_load_docling_artifact` requires a `file_hash` match, or a content match that includes the page count. The verdict records `file_hash_match`. |
| PDF page count known | `unverifiable / pdf_page_count_unknown` |
| Requested range inside the PDF | `missing / range_outside_document` |
| Row `page_count` equals PDF pages | `unverifiable / page_count_mismatch` |
| Page ids are integers, unique, and agree with `pages[k].page_no` | `non_integer_page_id`, `duplicate_page_identifiers`, `conflicting_page_identifiers` |
| Every `prov.page_no` is a listed page | `prov_page_not_in_pages` |
| Chunk offsets | Used only when the producer **recorded** `page_offset`. Never derived. Invalid offsets are ignored. |
| Chunked and not the full page set | `unverifiable / chunk_relative_numbering` |
| Pages 1..N of a longer PDF, no record of how produced | `unverifiable / ambiguous_truncated_numbering` |
| Gaps that stop short of the last page, without an explicit `used_chunking: false` | `unverifiable / ambiguous_truncated_numbering` |
| Coverage of the requested pages | `valid` (all present), `partial / span_partially_covered`, `missing / span_not_covered` |

A gapped whole-document conversion is accepted as absolute only when it reaches the last PDF page, or when the row explicitly says it was not chunked. Original numbers are never guessed.

### 2.2 Fallback routing (`BulkExtractor._structured_representation`)

The five representation states from the brief map to these behaviors:

| State | `docling` mode | `hybrid` mode | Recorded |
|---|---|---|---|
| 1. Valid structured representation | Docling | Docling | `docling_page_integrity.status=valid` |
| 2. Valid, no tables in the span | Docling (unchanged) | Docling (unchanged) | `docling_page_integrity.tables=0` |
| 2b. Valid, but the span holds no items | Empty rep (unchanged) | **native** | `docling_fallback_reason=empty_slice` |
| 3. Partial (verified numbering, some pages missing) | Docling, marked `degraded` (→ `degraded_structured_input` review) | **bounded / native** | `span_partially_covered` |
| 4. Unverifiable original-page mapping | **None** (no structure) | **bounded / native** | `chunk_relative_numbering`, etc. |
| 5. Missing (no artifact, empty doc, span not covered) | None | bounded / native | `no_artifact`, `span_not_covered`, ... |

- Docling mode deliberately does not switch backend. It is the "Docling only" benchmark arm. Its native-table fallback is hybrid mode.
- Legacy text fallback is untouched. `_slice_docling_text`, `docling_artifact_is_degraded`, `_merge_chunk_results` and `convert_pdf_safe` were not edited.

### 2.3 Candidate provenance

- `_reject_unverified_evidence` collects every `page` an evidence record cites, at any nesting level (`values`, `cells`, `label`, `header`, `date`). It drops evidence that cites a page the representation in use does not hold.
  - Evidence is derived from that representation, so for a verified one this removes nothing. Benchmarks confirm it: the committed-cache runs are identical.
  - It is a guard against page references reaching a candidate from anywhere else.
  - It is reported in `structured_input.rejected_evidence`, with candidate finding `structured_evidence_rejected` (level review, stored as warning).
- Candidate runs add `docling_artifact_unusable` (level info) when an artifact was bypassed. It is info because the structure actually used is complete for its own backend. `degraded_structured_input` stays reserved for genuinely partial input.
- No page numbers or bboxes are synthesized. Rejected spans get structure from native (or bounded) conversion of the real pages.
- Candidate-only persistence, explicit promotion and rollback are unchanged.

## 3. Modules changed

| File | Change |
|---|---|
| `src/duke_rates/document_intelligence/page_integrity.py` | **New.** `assess_docling_page_integrity`, `PageIntegrity` |
| `src/duke_rates/document_intelligence/page_integrity_audit.py` | **New.** Read-only exposure audit |
| `src/duke_rates/historical/ncuc/pipeline/bulk_extractor.py` | New `_docling_selection`, `_pdf_page_count`, `_structured_integrity_details`, `_evidence_pages`, `_reject_unverified_evidence`. `_docling_representation` is now a wrapper (signature kept). `_structured_representation` gains an optional `diagnostics` argument and the routing above. `_with_structure` passes diagnostics. `extract_charges_from_document`: 2 lines (§4.3). |
| `src/duke_rates/historical/ncuc/pipeline/candidate_runs.py` | `record_candidate_run`: two findings |
| `src/duke_rates/cli_commands/provenance.py` | **New command** `provenance audit-docling-coverage --db --json` |
| `bench/run_extraction.py` | `--docling-source {cache,db}`, default `cache` (unchanged) |
| `bench/phase2e/contamination_probe.py` | **New.** Extract-only probe over audit documents |
| `tests/test_page_integrity.py` | **New**, 27 tests |
| `tests/test_docling_page_integrity_extraction.py` | **New**, 15 tests, including the Leaf 600 integration test |
| `tests/test_audit_docling_coverage.py` | **New**, 3 tests |
| `tests/test_bounded_extraction.py` | One monkeypatch moved from `_docling_representation` to `_docling_selection` (the seam moved; same assertion) |
| `tests/cli_command_surface.snapshot.txt`, `tests/test_cli_command_surface.py` | +1 command (353 → 354) |

**Divergence from the plan:** the coverage helper lives in a new `document_intelligence/page_integrity.py` rather than `db/artifact_cache.py`. It is pure and is used by both the extractor and the audit. The plan's `docling_page_coverage` fields map onto `PageIntegrity`: `pages` → `covered_pages`, `absolute` → `numbering_verified`, `complete` → `usable`.

## 4. GitNexus

### 4.1 Re-index

Re-indexed with `npx gitnexus analyze`. It needed `GITNEXUS_LBUG_BUFFER_POOL_SIZE=2147483648`; the default 256 MiB pool ran out. The analyzer rewrote `CLAUDE.md` and `AGENTS.md`. Both were restored with `git checkout`.

### 4.2 Impact (upstream)

| Symbol | Risk | Direct callers |
|---|---|---|
| `_docling_representation` | LOW | 1 |
| `_structured_representation` | LOW | 1 (flows: bench `main`, `mine_tariff_sheets_nc`) |
| `extract_charges_from_document` | **CRITICAL** | 4 (`mine_tariff_sheets_nc`, `diagnose_document_nc`, `mine_docling_nc`, bench `main`) |

### 4.3 The CRITICAL edit

I edited the structured-only block of `extract_charges_from_document` **before** running impact on it. Once I saw the risk, I moved the logic into `_structured_integrity_details`. The hub's diff is now one call plus `| integrity_details` on the `structured_input` dict, inside `if self.structured_mode != "legacy":`.

The legacy branch is untouched. Committed-cache legacy output is field-identical, including evidence pages.

`detect_changes` (compare `219a88a..HEAD`) reports overall risk "high" because of this hub. The edited symbols are exactly the ones in §3. The other "touched" symbols are line-shift artifacts of code inserted above them, not edits: `_find_tariff_type_in_pages`, `_should_force_tariff_for_bounded_leaf`, `_is_reference_only_document`, `_bounded_representation`, `_attach_located_evidence`, `unsupported_charges_cmd`. The MCP server still served the pre-reindex graph ("24 commits behind").

## 5. Regression-test results

| Suite | Result |
|---|---|
| `test_page_integrity.py` | 27 passed |
| `test_docling_page_integrity_extraction.py` | 15 passed |
| `test_audit_docling_coverage.py` | 3 passed |
| Structured-extraction set (`test_structured_extraction`, `test_docling_artifact_selection`, `test_bounded_extraction`, `test_bulk_candidate_pipeline`, `test_structured_profiles`, `test_docling_bridge_integration`, `test_extraction_provenance`, `test_bulk_extractor_fallback_guard`, plus the new files) | 156 passed |
| Full suite (`pytest -q`) | 1,718 passed / 92 skipped / 4 xfailed / **2 failed** before the snapshot update (`test_cli_command_surface`, the new command). After the update: 2 passed. Output: `reports/phase2e_m1/pytest_full.txt` (run before the snapshot fix). |
| Frozen 12-bill baseline (`test_billing_baseline.py`, in the full suite) | passed |
| Billing gate (`bench/billing_check.py`, before vs after) | identical (§6.4) |

Coverage of the brief's list:

| Brief case | Test |
|---|---|
| Correct whole-document artifact | `test_whole_document_artifact_is_valid_for_any_range`, `test_complete_artifact_keeps_original_page_numbers` |
| Correct bounded range | `test_bounded_range_inside_a_complete_artifact` |
| Chunked, relative numbers | `test_chunked_artifact_with_relative_numbers_is_unverifiable`, `test_chunk_relative_artifact_is_rejected_with_a_reason` |
| Only the final chunk | the two above (artifact 1661 shape) |
| Span outside coverage | `test_requested_range_outside_the_pdf`, `test_span_not_covered_and_partially_covered_with_absolute_gaps`, `test_span_beyond_the_chunk_no_longer_suppresses_native` |
| Duplicate page ids | `test_duplicate_page_identifiers_are_unverifiable` (and `conflicting_…`, `non_integer_…`) |
| Missing original-page metadata | `test_missing_original_page_metadata_is_unverifiable`, `test_truncated_artifact_without_metadata_is_ambiguous` |
| Valid, no tables | `test_valid_artifact_without_tables_is_kept` |
| Native fallback after rejection | `test_hybrid_falls_back_to_native_and_docling_mode_does_not` |
| No wrong-page contamination in hybrid | `test_leaf600_contamination_is_blocked_in_hybrid`, `test_leaf600_contamination_is_blocked_in_docling_mode`; `test_fixture_reproduces_wrong_page_contamination_without_the_guard` proves the fixture reproduces the defect |
| Correct candidate page provenance | `test_accepted_candidates_keep_correct_source_pages`, `test_evidence_citing_pages_outside_the_representation_is_dropped`, `test_candidate_runs_record_artifact_fallback_findings` |

## 6. Benchmark comparison

All runs used `.venv312` on a scratch copy (`m1.db`) of production. "Before" is a detached worktree at `219a88a`, driven by the same bench scripts.

### 6.1 Committed Docling cache (gate: identical)

`bench/phase2d/diff_runs.py --evidence` against `reports/phase2c/bench_{mode}.json`:

| Mode | Verdict |
|---|---|
| legacy | IDENTICAL |
| native | IDENTICAL |
| docling | IDENTICAL |
| hybrid | IDENTICAL |

The bench cache uses its own representation provider. M1 cannot change it, and it did not.

### 6.2 Production artifacts (`--docling-source db`)

Scores: `reports/phase2e_m1/bench/scores.md`.

| Metric | hybrid, before | hybrid, M1 | docling, before | docling, M1 |
|---|---:|---:|---:|---:|
| Recall | 0.7719 | 0.7719 | 0.7607 | 0.7607 |
| Precision | 0.9809 | 0.9809 | 0.9806 | 0.9806 |
| TOTAL reconciliation | 0.7179 | 0.7179 | 0.7179 | 0.7179 |
| Zero-doc false positives | 0 | 0 | 0 | 0 |
| Page provenance | 0.8646 | **0.8694** | 0.021 | 0.021 |

- Hybrid with production artifacts (0.7719) is below committed-cache hybrid (0.7732). The one-charge gap is on `dep_blk13_sub1300` and is the same before and after M1, so it comes from the production artifact, not this change.
- **Field diff before vs after:** docling identical. Hybrid differs on **one document, hd 3140** (charges identical; 6 evidence pages). Before, the chunk-relative structure for span 4–6 gave the value locator nothing to anchor on. After, native evidence on page 5, inside the span, is found for 3 charges.

The four benchmark documents on truncated artifacts:

| Doc | Span | Backend before → M1 | Fallback reason | Charges | Evidence outside span |
|---|---|---|---|---|---|
| hd 7378 | 59–67 | none → none | (no structure requested; empty doc) | 0 → 0 | 0 |
| hd 3140 | 4–6 | (rep not stored) → native evidence | `chunk_relative_numbering` | 6 → 6 | 0 |
| hd 7365 | 27–41 | docling → **native_pdf** | `chunk_relative_numbering` | 0 → 0 | 0 |
| hd 0378 | 156 | docling → **native_pdf** | `chunk_relative_numbering` | 0 → 0 | 0 |

Hybrid recall on these four equals the committed-cache native-mode recall: 6 / 0 / 0 / 0 charges.

### 6.3 Contamination probe (177 exposed documents with versions)

Hybrid, extract-only. Files: `reports/phase2e_m1/contamination_{before,after}.json`.

| | Before | After |
|---|---:|---:|
| Documents (errors) | 177 (0) | 177 (0) |
| Charges | 1,763 | 1,756 |
| Charges with Docling evidence from an unverifiable artifact | **124** (9 docs) | **0** |
| Charges with any evidence | 124 | 1,153 (108 docs gain in-span native evidence) |
| Evidence outside the span | 0 | 0 |
| Backend for docs whose profile loaded structure | docling 29 | native_pdf 29 |
| Identical charge sets | — | 171 / 177 |

"Evidence outside span" is 0 both times. The wrong pages were relabeled inside the span, so only the integrity check can see them.

The six documents whose charges changed:

| hd | Family, span | Change | Precise reason | Assessment |
|---|---|---|---|---|
| 2275 | leaf-600, p.1 | 24 → 0 | Page 1 is the Index of Tariffs. All 24 were table-only rows from the chunk (Leaf 600 from PDF ~p.100) labeled page 1. | **Confirmed false positives removed** |
| 7265 | leaf-600, p.1 | 105 → 104 | Page 1 is again the index. The leaf-600 profile reads the PDF directly and finds the real Leaf 600, matching legacy and production (104). Before: wrong-page Docling tables of the same leaf supplied `$/kWh` for 9 Traffic Signal rows plus a TOTAL. After: native geometry tables turn them into `$/kW` (×100) and drop the TOTAL. | **Native-path regression.** Traffic Signal is energy-only, so `$/kW` is very likely wrong. Needs a fix before promotion. |
| 7724, 3687, 5365 | leaf-602 (JAA), 1–4 pages each | 8 → 3 each | Before: an empty Docling slice made the structured JAA parse find nothing, so the text parse (8 rates, as in production) stood. After: native geometry finds only the 3 kWh-class rows, and the structured JAA path replaces the text parse. Other leaf-602 documents (hd 725, 7247, 818) keep 8 with native. | **Native-path regression**, 15 rates. The JAA structured/text merge should not drop text rates the table lacks. |
| 536 | DEC rider summary (Leaf 99), p.8–12 | 78 → 111 | Before: wrong-page Docling structure. After: native tables add 12 distinct keys, repeated ×4 (BPM-P, BPM-T, Coal Inventory, Cost of Removal …). Production has 141 rows. | **Needs review.** It looks like the same summary on several pages counted per page. Not proven wrong. |

These changes are what the plan asked for: an unusable Docling slice falls back to native. They expose existing weaknesses of the native fallback on these layouts. They are candidate-only: nothing is promoted, and production is unchanged.

### 6.4 Billing gate

`bench/billing_check.py` (12-bill DEP reconciliation) ran on a separate scratch copy (`m1_billing.db`) with the pre-M1 and M1 trees. The summary and per-bill results are **identical**: 12 bills, 11 `good_match`, 1 `needs_review`, max |Δtotal| $0.45 (`reports/phase2e_m1/billing_{pre_m1,m1}.json`). This is expected: M1 changes no DB content the billing engine reads.

## 7. Exposure audit

Run with `duke-rates provenance audit-docling-coverage --db data/db/duke_rates.db --json reports/phase2e_m1/docling_page_integrity_audit.json` (opened `mode=ro`).

| Quantity | Count |
|---|---:|
| Docling artifacts with `doc_json` | 1,962 |
| Valid | 1,855 |
| **Unverifiable (`chunk_relative_numbering`)** | **107** (107 PDFs) |
| Affected historical documents (all NC) | **229** |
| … with a tariff version | 177 |
| Affected tariff versions (distinct; 3 documents bind 2 each) | **180** |
| Span class: `wrong_pages` / `not_covered` / `unbounded` | 44 / 183 / 2 (with version: 41 / 134 / 2) |
| **Possible false-positive exposures** (version, span overlaps chunk numbers or unbounded) | **43** |
| **Missing-structure exposures** (version, span beyond chunk) | **134** |
| Classification: `unsafe_page_mapping` / `incomplete_structured_coverage` / `no_structured_extraction_required` | 43 / 134 / 52 |
| `safe_for_requested_page_ranges` (bound to an unsafe artifact) | 0 |
| Requires regeneration (documents with versions) | 177 |
| Legacy slicer would accept an unsafe artifact | **0** |
| Production `tariff_charges` on exposed versions | 2,031 |
| `docling_page_miner_v1` page rows on the 107 PDFs | 3,787 (27 PDFs) |

This reproduces the reconciliation's 107 / 229 / 44 / 183 / 2 and 41 / 134. The plan's "175 with versions" excludes the 2 unbounded documents.

**Potential vs confirmed exposure:**
- **Confirmed corrupted production charges: none attributable to this defect.** Production `tariff_charges` were written by the legacy path. Production has no `extraction_runs` schema, and the structured path never ran on it. The legacy text slicer rejects all 107 artifacts by its own heuristic. Unbounded legacy reads `plain_text`, which the chunk merge keeps complete.
- **Confirmed contamination in the pre-M1 structured path:** 124 charges in 9 documents (§6.3), including 24 outright false-positive rows (hd 2275). These existed only in scratch candidate extraction.
- **Docling page-miner rows (Unverified provenance).** The 3,787 `docling_page_miner_v1` rows on these PDFs cover every page 1..N of each PDF. They therefore were not mined from the truncated 1..k `doc_json`. Their source was not traced in M1.

## 8. Remaining unsafe historical artifacts

- All **107** chunked rows remain in production, unchanged.
- Any structured consumer other than `BulkExtractor` that reads `doc_json` by page number is still unguarded. Known ones:
  - `docling_page_miner.mine_pages_from_docling_artifact`;
  - `bench/docling_page_miner_coverage.py`;
  - `scripts/analysis/fingerprint_docling_artifacts.py`;
  - the `doc_intel` CLI paths that call `representation_from_docling`.

  The validator is reusable for them.
- `convert_pdf_safe` can still write new truncated artifacts whenever a conversion falls back to chunking. M1 makes them harmless to extraction but does not stop them being written.

## 9. Recommended regeneration strategy

1. **Do not reconvert with `convert_pdf_safe`.** Its chunk merge re-creates the defect. Reconvert through `bounded.py` (page-correct, records `degraded` chunks). That is M6 in the reconciliation roadmap.
2. **Reconvert only what is consumed.**
   - The 177 documents with versions span 107 PDFs, but only their **spans** are needed. Bounded conversion of the spans is far cheaper than whole bundles, which run up to 316 pages.
   - Prioritize the 43 `unsafe_page_mapping` documents, then the 134 `incomplete_structured_coverage` ones.
   - Skip the 52 with no version.
3. **Write new rows, never overwrite.**
   - New artifacts need a backend version / options fingerprint (M3), so old and new coexist.
   - Every new row must pass `assess_docling_page_integrity` for the whole PDF (or for its recorded span) before it is used. The audit command is the acceptance check: zero `unverifiable` rows produced by the new path.
4. **Retire the 107 rows** only after their replacements validate, and only on explicit approval. Until then, M1 keeps them out of structured extraction.
5. **Do it after Docling Stage 1/2** (layout model in the cache key; Egret-medium decision). Then the reconversion is done once, under the configuration that will be promoted.

## 10. Suggested M2 scope

1. **Native-fallback quality (blocking promotion for the affected families):**
   - JAA structured/text merge must not drop text rates absent from the table (hd 7724 / 3687 / 5365);
   - Traffic Signal unit assignment in the Progress adjustment-matrix structured branch (hd 7265);
   - per-page duplicate rows in the DEC rider-summary native path (hd 536).

   Add these four documents to the benchmark corpus as hybrid regression cases.
2. **Guard the other `doc_json` consumers** (§8) with `assess_docling_page_integrity`. In particular, `docling_page_miner` must not write page artifacts from unverifiable `doc_json`.
3. **Stop writing truncated artifacts:** make `convert_pdf_safe`'s chunk path either keep every chunk's document with its offset or record `page_offset`. Its GitNexus risk is CRITICAL, so it is a separate change with its own impact review. Alternatively, route batch conversion through `bounded.py` (M6).
4. **`document_metadata["page_coverage"]` on every adapter** (reconciliation §8). M1 records the verdict only for Docling representations.
5. **Segmentation ground truth** (reconciliation M2), as planned, for boundary-level metrics, including "evidence page within the gold span".

## 11. Production integrity and scratch data

| Check | Result |
|---|---|
| Production SHA-256 before | `fe563a783cc78d6bb1a561685f1d83c81d7ea4c87729472ede96e192e888cd08`, mtime 2026-10-26 12:36:11 (`reports/phase2e_m1/production_sha_before.txt`) |
| Production SHA-256 after | `fe563a783cc78d6bb1a561685f1d83c81d7ea4c87729472ede96e192e888cd08`, mtime unchanged: **identical** (`reports/phase2e_m1/production_sha_after.txt`) |
| Production `-wal` | 0 bytes (scratch copies are complete) |
| Writes | Only to scratch copies (`m1.db`, `m1_billing.db`) and test temp DBs |
| Pushed / merged | No |

Scratch artifacts, in the session scratchpad and not committed: `m1.db`, `m1_billing.db`, worktree `pre_m1` @ `219a88a`.

## 12. Commits

| Commit | Content |
|---|---|
| `443af15` | `page_integrity` + unit tests |
| `84f3eb2` | Extractor guard, hybrid fallback, provenance guard, candidate findings, integration tests |
| `1d1e88c` | Read-only exposure audit + CLI + tests + production audit JSON |
| `5a10690` | `--docling-source db` + benchmark outputs |
| `f5f11a9` | CLI surface snapshot |
| `661fd29` | Contamination probe + before/after results |
| (this commit) | This report, production SHA after, billing gate |
