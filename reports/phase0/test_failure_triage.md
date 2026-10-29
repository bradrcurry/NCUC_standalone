# Phase 0: Test Failure Triage

Source: `reports/phase0/pytest_baseline_raw.txt` (23 failures, plus 1 collection error).
This was a read-only investigation. No source, test or DB file was modified. The live-data tests were checked against the frozen snapshot
(`scratchpad/test_snapshot.db`) and, for history, against `data/db/duke_rates_eval_20260511_162535.db`
(the 2026-05-11 eval copy, opened with `mode=ro`). Hypotheses were confirmed by re-running tests with throw-away pytest
plugins that live in the scratchpad (`-p <plugin>`). These plugins patch the seam the code actually uses, and the test files were not changed.

Note on git history: the repo begins at `09b83db` (2026-05-06, "Initial project import"). A change that already appears in that commit has no
finer-grained commit to cite. For those, the evidence is the code's own docstrings and comments.
Note: at the time of writing, `src/` had uncommitted edits made by someone else (docling artifact selection in bulk_extractor, docling_page_miner, ocr,
artifact_cache). None of those edits touch the code paths below. Line numbers are as of this write-up.

## Summary table

| # | Test | Category | Root cause (short) | Recommended fix | Risk |
|---|------|----------|--------------------|-----------------|------|
| 1 | test_dep_validation_analytics::test_dep_validation_report_identifies_expected_clean_rider_gaps | 4 Mutable prod data (plus one small code gap) | Snapshot has 2 partial clean-rider dates. 2024-12-01: no BA-EMF line in DEP RES blocks 393/400/407. 2026-06-01: block 442 (ingested 2026-06-05) has no EDIT-4 because EDIT-4 ended 6/1/2026 | Add an EDIT-4 retirement date (2026-06-01) next to RAL-2 in dep_validation.py. Make the test assert against a frozen fixture or an explicit known-gap allowlist | Low |
| 2 | test_document_intelligence::test_bulk_extractor_record_parse_attempt_persists_document_intelligence | 2 Outdated test | `record_parse_attempt` takes a required keyword-only `text_metrics`, and the test omits it | Pass `text_metrics=None` in the test, or give the parameter a default of `None` (bulk_extractor.py:2083) | Low |
| 3 | test_historical_parser_profiles::test_registry_selects_progress_single_value_rider_for_leaf_602_with_relaxed_family_signal | 2 Outdated test | Commit d5e6428 added `ProgressJaaRiderProfile` and removed leaf-602 from the SingleValue SUPPORTED/RELAXED sets on purpose | Assert `ProgressJaaRiderProfile` | Low |
| 4 | test_historical_parser_profiles::test_bulk_extractor_forces_tariff_for_authenticated_portal_progress_rider | 2 Outdated test | Same as #3, d5e6428. The run inserts ≥4 charges, and only the profile name differs | Expect `"progress_jaa_rider"` | Low |
| 5 | test_historical_parser_profiles::test_bulk_extractor_skips_reference_only_program_families_as_accepted | 2 Outdated test (stale monkeypatch seam) | The test patches `extractor.classify_document`, but `process_document` now calls `self.classifier.classify_with_result`. The real classifier returns "other", so the doc exits as `skipped_other` before the reference-only check | Patch `extractor.classifier.classify_with_result` instead. Confirmed: with that patch the test passes | Low |
| 6-9 | test_ncuc_pipeline::test_page_miner_extracts_descriptive_rider_heading, `_with_parenthetical`, `inline_descriptive_heading_from_merged_pdf_text`, `descriptive_program_heading` | 2 Outdated test | Commit c561dbe added the `_is_likely_code()` gate in `_expand_heading_codes` (page_miner.py:115-142, 176). Full descriptive headings are no longer stored as schedule codes | Update the assertions to the new contract. Decide whether descriptive headings belong in a separate field (see notes) | Low for the test. The design follow-up is Medium |
| 10 | test_ncuc_pipeline::test_importer_reintegrates_ocr_required_documents_via_cpu_ocr | 2 Outdated test (stale monkeypatch seam) | The test patches `importer_module.extract_ocr_document_pages`, but the importer calls `extract_pages_with_progressive_ocr`. Real OCR backends then try to open a 9-byte stub `b"%PDF-1.4\n"` and fail | Patch `importer_module.extract_pages_with_progressive_ocr` to return `(pages, "pytesseract_cpu", [...])`. Confirmed: passes in 2.9s instead of 16.8s | Low |
| 11 | test_reprocess_queue::test_process_reprocess_queue_bootstraps_missing_tariff_version | 2 Outdated test | Commit 8923ed9 (2026-05-21) made the queue worker call `process_document(doc, force_clear=...)`. The fake `_fake_process_document(self, doc)` raises TypeError, so the queue item is marked failed | Add `force_clear: bool = False` to the fake | Low |
| 12 | test_rider_trust::TestLoadRiderTrustTable::test_all_rate_class_groups_present | 2 Outdated test | `load_rider_trust_table` now loads 7 groups by design (adds dep_mgs_nd, dep_mgs_d, dep_lgs, per the docstring at rider_trust.py:85). The test still lists 4 | Extend `_VALID_GROUPS` to the 7 groups (tests/test_rider_trust.py:30) | Low |
| 13-22 | test_tariff_engine_live (TestRTouLive x4, TestRToudLive, TestSgsToueLive x2, TestSgsLive x2, TestCrossScheduleSanity x2) | 4 Mutable prod data | The hard-coded rates and charge rows no longer exist in the selected versions. Charges were re-extracted on 2026-05-29 (4,334 of 5,198 snapshot rows date from then): leaf-502 lost its fixed/off-peak/discount rows, leaf-503 is now tagged `season='summer'`, and SGS charges are tagged `commercial_small`. Older SGS/SGS-TOUE versions (2022-12 and 2023-01) now win version selection | Convert to frozen fixtures that seed the expected versions and charges. Separately, open data-quality tickets for leaf-502 and leaf-503 | Low (fixture). The data fixes are separate |
| 23 | tests/test_baseline_classifier.py (collection error) | 3 Environment/dependency | `baseline_classifier.py` imports sklearn, which is not installed in .venv312 and not declared in pyproject.toml | Add an `ml = ["scikit-learn>=1.4"]` extra, and add `pytest.importorskip("sklearn")` in the test module | Low |

No failure was classified as category 5 (nondeterministic). Every failure reproduced the same way on each run. No failure is a straightforward application defect where the test is right. The closest cases are the latent issues flagged in notes #1, #6-9 and #13-22.

---

## Per-test notes and evidence

### 1. test_dep_validation_report_identifies_expected_clean_rider_gaps (category 4)

Actual result: `partial_clean_rider_dates == ['2024-12-01', '2026-06-01']`. Rows from `load_dep_res_validation_report(snapshot)["clean_rider_validation"]`:

| effective_date | missing | quality_flag | source |
|---|---|---|---|
| 2024-12-01 | BA-EMF | derived_from_partial_components | e-2-sub-1345/0bfd8fc6...pdf (block 407; 393 and 400 are identical copies) |
| 2026-06-01 | EDIT-4 | ok | data/raw/nc/progress/rider/summary-of-rider-adjustments-...leaf-no-600-s.pdf (block 442) |

Evidence:
- `SELECT ... FROM rider_summary_blocks WHERE rate_class='Residential Service Schedules' AND effective_date='2024-12-01'` returns blocks 393, 400 and 407. All three have `total_cents_per_kwh = NULL`. Their line items are BA, BA-Fuel, BA-DSM, BA (net), EDIT-4, JAA, CPRE, CAR, RDM, ESM, PIM, with no BA-EMF. The dates are garbled redline overlays (`'12/1/234'`). This was already true in the 2026-05-11 eval DB, so it is an old extraction gap and not a regression.
- Block 442 (2026-06-01) has `created_at = 2026-06-05T19:19:34`. It came from the June-2026 web-update ingest. EDIT-4 is legitimately absent because EDIT-4 terminated on 6/1/2026 (DEP leaf-604). The validator only knows about one retirement: `RAL2_RETIREMENT_DATE = "2024-10-01"` (dep_validation.py:16, used in `_expected_clean_rider_codes_for_date` at about line 208). So it still expects EDIT-4.
- The same result came from the pre-`8b77aa4` code (`git archive d6a0cb3`, run against the snapshot), so the latest commit did not cause this.

Recommended fix:
- (a) Code (small, real gap): add an `EDIT4_RETIREMENT_DATE = "2026-06-01"` and exclude EDIT-4 for dates on or after it, the same way RAL-2 is handled. Expose it in `retired_clean_rider_codes`. Low risk.
- (b) Test: either keep it live and assert `set(partial) <= {"2024-12-01"}` with a comment, or convert it to a frozen fixture that seeds `rider_summary_blocks` and `rider_line_items` for the dates under test. Separately, the BA-EMF gap at 2024-12-01 needs a source-PDF re-parse of e-2-sub-1341/1344/1345 leaf-600.

### 2. test_bulk_extractor_record_parse_attempt_persists_document_intelligence (category 2)

`TypeError: record_parse_attempt() missing 1 required keyword-only argument: 'text_metrics'`. The signature is at bulk_extractor.py:2076-2088. `text_metrics: dict[str, Any] | None` has no default and sits between keyword-only arguments that do have defaults. It is persisted as `metadata["text_metrics"] = text_metrics or {}`. The only production caller (`process_document`, about line 2500) always passes it. Both the parameter and the test are already in `09b83db`, so the API change predates repo history. The test simply wasn't updated. Confirmed: forcing `text_metrics=None` (scratch plugin) makes the test pass.
Fix: add `text_metrics=None` to the test call. Alternatively, and slightly better, default the parameter to `None` in the code, since the body already handles `None`. Low risk either way.

### 3-4. JAA profile tests (category 2)

Intentional commit: **d5e6428** (2026-05-14, PR #9). Its message reads "Add ProgressJaaRiderProfile for leaf-602 JAA multi-class rate table ... removed leaf-602 from ProgressSingleValueRiderProfile SUPPORTED and RELAXED families so JAA always wins". The registry lists `ProgressJaaRiderProfile()` at parser_profiles.py:6993, and its `matches()` keys on `family_key == "nc-progress-leaf-602"`. In test #4 everything else passes (`inserted >= 4`). Only `parser_profile` differs (`progress_jaa_rider`).
Fix: assert `isinstance(profile, ProgressJaaRiderProfile)` and `row["parser_profile"] == "progress_jaa_rider"`. Optionally rename test #3, since "relaxed family signal" no longer applies to leaf-602. Low risk.

### 5. test_bulk_extractor_skips_reference_only_program_families_as_accepted (category 2: stale seam)

`process_document` classifies with `self.classifier.classify_with_result(doc['title'], text_sample)` (bulk_extractor.py:1344). It no longer calls `self.classify_document`, which is now only used inside `_find_tariff_type_in_pages`. The test's `monkeypatch.setattr(extractor, "classify_document", ...)` (test line 4156) therefore has no effect on the main gate. The real rule classifier labels the stub text "other", and the doc returns `f"skipped_{doc_type}"` (line 1451) before `_is_reference_only_document` runs (line 1458). The `classify_with_result` path documents itself as the intentional Phase-2 change (document_prep.py docstring: "Classify and return both the legacy string label and a ClassificationResult for the Phase 2 document_type stage"). It is present at `09b83db`.
Confirmed: a scratch plugin that forces `classify_with_result` to return "tariff" for this title makes the test pass (32s).
Fix: patch `extractor.classifier.classify_with_result` (return `("tariff", result)`) or `extractor.classifier.classify`. Low risk.
Design observation (not the cause): reference-only family membership is described as "authoritative" in `_is_reference_only_document`, but it is only checked after the classifier gate. Real leaf-740 and other `_REFERENCE_ONLY_FAMILIES` docs that the classifier labels non-tariff will be logged as `skipped_other` rather than `skipped_reference`/accepted. Moving the reference-only check ahead of the classifier short-circuit would make the outcome deterministic. That is a behaviour change, so Medium risk. Also, this test takes about 31s because it runs the embedding and LLM classifier recorders, which should be stubbed in unit tests.

### 6-9. Page-miner descriptive heading tests (category 2)

The function is `_expand_heading_codes()` in `src/duke_rates/historical/ncuc/pipeline/page_miner.py`, called from `_extract_page_features_from_text()`. The heading regexes `SCHEDULE_HEADING_REGEX` (line 13) and `INLINE_DESCRIPTIVE_HEADING_REGEX` (line 21) still match correctly:
- `"FUEL COST ADJUSTMENT RIDER"` matches the descriptive alternative `[A-Z][A-Z0-9&(),/\- ]{6,}(?:RIDER|SERVICE|SCHEDULE|PROGRAM)`.
- `"SMART ENERGY NOW PROGRAM (NC)"` matches `SCHEDULE_HEADING_REGEX`.
- The inline regex extracts `"FUEL COST ADJUSTMENT RIDER"` from the merged line.

The heading is then dropped by the gate `if _is_likely_code(candidate):` (line 176). `_is_likely_code` (lines 115-142) rejects text longer than 25 characters, and rejects headings unless every token matches `_CODE_TOKEN_RE`: a dashed code, a number, a 2-6 letter acronym, or alnum+digit. `FUEL`, `COST`, `ADJUSTMENT`, `ENERGY`, `PROGRAM` and so on fail.

Intentional commit: **c561dbe** (2026-05-11, "Staged extraction pipeline ... Phase 6E"). Its message says "Loosen _NON_CODE_TOKENS deny-list -> structural _CODE_TOKEN_RE". The diff replaces the unconditional `expanded.append(candidate)` with the gated append and adds the comment: "Descriptive phrases like 'APPLICABLE TO ELECTRIC UTILITY SERVICE' are NOT schedule codes and must not be stored." The four tests date from `09b83db` and were never updated.

Why the result is `['RIDER']`: the only surviving code comes from `metadata.extract_rider_codes`. `RIDER_CODE_PAT` = `\b(?:rider|clause|adjustment)\s+([A-Z]{1,3}\d*|[A-Z]+(?:-\d+)?)\b` (metadata.py:26, IGNORECASE) matches `"ADJUSTMENT RIDER"` and captures the word `RIDER`. That is a junk code. It is a pre-existing quirk, but it is now the only output for these headings.

Fix: update the tests to the new contract. For example, assert `has_schedule_heading is True`, assert the leaf number, and assert that no descriptive phrase is stored. Better, add a positive test for an explicit code (e.g. "RIDER BA" gives "BA").
Follow-ups (Medium risk, behaviour change):
- (a) `repository._load_current_document_page_signals` (repository.py:~197) builds `heading_tokens` from `page.extracted_schedule_codes` for family-match scoring. c561dbe therefore also removed descriptive heading tokens (FUEL, COST, ADJUSTMENT) from family matching. Consider a separate `extracted_heading_text` field so matching keeps those tokens.
- (b) Stop `RIDER_CODE_PAT` from capturing the literal word `RIDER`, or add a stop-word guard.

### 10. test_importer_reintegrates_ocr_required_documents_via_cpu_ocr (category 2: stale seam)

The test monkeypatches `importer_module.extract_ocr_document_pages`. But `NcucPipelineImporter.mine_discovery_record_spans` (src/duke_rates/historical/ncuc/importer.py:421+) calls `extract_pages_with_progressive_ocr(...)` (line ~452) in the OCR_REQUIRED branch. That function is imported by name at importer.py:38-41. The patched symbol is therefore never called, and the real progressive OCR chain runs on the test's stub file.

Why the PDF fails to open: the test writes `pdf_path.write_bytes(b"%PDF-1.4\n")`, which is a 9-byte header with no objects, no xref, no trailer and no `/Root`. It is a placeholder, not a parseable PDF. Each backend rejects it:
- ocrmypdf/pytesseract via PyMuPDF: `Failed to open file '...ocr-required.pdf'`
- Docling via pypdfium2: `PdfiumError: Failed to load document (PDFium: Data format error)`
- GLM-OCR: the same PyMuPDF open failure

`extract_pages_with_progressive_ocr` returns no pages, so the importer logs "could not be OCR-mined" and returns `[]`, and `len(created_ids) == 0`. The progressive path is already in `09b83db`.
Confirmed: a scratch plugin that routes `importer_module.extract_pages_with_progressive_ocr` to the test's fake (returning `(pages, "pytesseract_cpu", ["pytesseract_cpu"])`) makes the test pass in 2.9s, down from 16.8s.
Fix: in the test, monkeypatch `extract_pages_with_progressive_ocr` with that three-tuple signature. The test name can stay. Low risk.

### 11. test_process_reprocess_queue_bootstraps_missing_tariff_version (category 2)

The queue row ends with `status='failed'`, and `error_message` = "_fake_process_document() got an unexpected keyword argument 'force_clear'" (dumped by a scratch plugin). The worker calls `extractor.process_document(doc, force_clear=force_clear)` (src/duke_rates/cli_commands/reprocess.py:272). Intentional commit: **8923ed9** (2026-05-21, PR #10, "Cross-attribution cleanup workflow: force_clear ... process_document accepts and forwards force_clear ... threads it through the queue worker"). Commit e74d2fe later moved it into cli_commands/reprocess.py. The bootstrap assertions earlier in the test still pass.
Fix: `def _fake_process_document(self, doc, force_clear=False)` (tests/test_reprocess_queue.py:1003). Low risk.

### 12. TestLoadRiderTrustTable::test_all_rate_class_groups_present (category 2)

`load_rider_trust_table` (src/duke_rates/analytics/rider_trust.py:101-109) loads seven groups by design. The docstring at line 85 enumerates `'dep_residential' | 'dep_sgs' | 'dep_sgs_clr' | 'dep_mgs_nd' | 'dep_mgs_d' | 'dep_lgs' | 'dec_residential'`. The test (`_VALID_GROUPS`, line 30) lists four. On the snapshot, the MGS-ND, MGS-D and LGS loaders return 91, 55 and 56 rows. Their `rider_summary_blocks` have existed since 2026-03-19, and they are present in the 2026-05-11 DB too, so this is not a recent data change. The loaders and docstring are in `09b83db` alongside the stale test. The working-tree diff to this test only swaps in the conftest snapshot path.
Fix: set `_VALID_GROUPS` to the 7 groups. A looser alternative is `assert {4 core groups} <= groups <= {7 groups}`. Low risk.

### 13-22. test_tariff_engine_live, 10 tests (category 4)

The module docstring pins exact rates that were "computed from the live charge rows", for example R-TOU fixed=$14, on=0.29905, off=0.11321, disc=0.07372. Snapshot state (`tariff_versions` / `tariff_charges`, read-only):

| family | ref date | selected version | snapshot charges | why the test fails |
|---|---|---|---|---|
| leaf-502 R-TOU | 2025-11-01 | 5307 (2025-10-01) | **1 row**: tou_energy on_peak 0.29905 (created 2026-05-29). There is no fixed $14, no off-peak and no discount | 0.11321 and 0.07372 appear nowhere in the snapshot for leaf-502/503/520/521. The bill is 89.72 (on-peak only). No fixed item. 1 TOU item instead of 3 |
| leaf-503 (title now "...Time-of-Use with Critical Peak Pricing"; the test calls it R-TOUD) | 2025-11-01 | 5313 (2025-10-01) | 4 tou_energy rows, **all `season='summer'`**. In the 2026-05-11 DB the same rows were `all_year`. There is no fixed charge | November is winter, so every charge is filtered out and the total is 0.0 |
| leaf-521 SGS-TOUE | 2025-08-01 | 5935 (2023-01-01) | $21/$7 fixed and summer/winter TOU, all `customer_class='commercial_small'` | The expected $22 / 0.19463 / 0.09721 / 0.07624 live on v5320, dated 2025-10-01, which is not eligible for Aug 2025. The test calls with the default `customer_class="residential"`, and `_charge_applies` excludes `commercial_small`, so the total is 0.0 |
| leaf-520 SGS | 2025-01-01 and none | 5934 (2023-01-01) / 5318 (2025-10-01) | All rows `customer_class='commercial_small'` (were NULL on 2026-05-11). 0.13188 is on v5318 only | Residential default filters everything out, so the total is 0 and there is no seasonal warning (all rows are `all_year`) |
| leaf-504 | 2025-11-01 | 5317 (2026-01-01, future, so earliest wins) | Only a three-phase surcharge plus `charge_type='energy'` standard/discount rows (duplicated). The $14 basic charge from 2026-05-11 is gone | The comment's premise ("only a discount period parsed" as tou_energy) no longer holds. The engine ignores `charge_type='energy'`, so there is no "Partial TOU coverage" warning |

History: 4,334 of the 5,198 snapshot `tariff_charges` rows have `created_at` 2026-05-29. This is the late-May cleanup and re-extraction (the 2026-05-11 DB had 26,638 rows). The test file is unchanged since `09b83db` apart from the conftest snapshot swap. Even the 2026-05-11 DB lacked leaf-502's fixed/off/discount rows. The tests have tracked mutable production state since before the repo existed.
Fix: convert to a frozen-fixture test. Seed a temp DB with families 502/503/504/520/521/523, one version each with the documented charges (e.g. 502: fixed 14 $/month; tou_energy on_peak 0.29905, off_peak 0.11321, discount 0.07372, all_year, residential/NULL class), and run the engine on it. Pass `customer_class="commercial_small"` for the SGS cases. Low risk.
Separate data and code tickets, not needed for the test fix:
- (a) leaf-502 2025-10-01 re-extraction kept only 1 of 4 charges.
- (b) leaf-503 2025-10-01 charges were mis-tagged `summer`.
- (c) The engine silently drops `charge_type='energy'` (24 rows in the snapshot) and the new non-canonical types (`'Energy Charge'`, `'Fixed Monthly Charge'`, `'Rider Adjustment'` and others). It only handles `energy_block`/`tou_energy`. It should at least warn. This is a genuine latent defect.
- (d) `Repository(...)` opens the snapshot with `duke_rates.db.sqlite.connect`, which runs migrations, so these tests can mutate the snapshot schema. They should use a copy.

### 23. tests/test_baseline_classifier.py, collection error (category 3)

`src/duke_rates/classification/baseline_classifier.py:22-25` imports `sklearn` (TfidfVectorizer, LogisticRegression, metrics, model_selection). The module was added in **b010eb2** (2026-05-21, PR #11). `import sklearn` raises `ModuleNotFoundError` in .venv312, and no extra in `pyproject.toml` declares scikit-learn. The CLI imports it lazily (cli_commands/doc_intel.py:3898), but the test imports it at module level.
Fix: add `ml = ["scikit-learn>=1.4"]` (with numpy) under `[project.optional-dependencies]` and install it in .venv312. Put `pytest.importorskip("sklearn")` at the top of the test module so environments without the extra skip cleanly. Low risk.
