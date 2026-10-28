# Phase 2E M1 execution plan: structured-input integrity for production Docling artifacts

**Base:** branch `phase2e/document-intelligence-reconciliation`. Work on a new branch `phase2e/m1-structured-input-integrity` cut from it.
**Background:** `docs/reports/2026-10-28_architecture_reconciliation.md` §1.2, §5, §10.4.

## Problem (confirmed 2026-10-28, read-only)

- **Truncated artifacts.** 107 `docling_artifacts` rows with `status='success'` were written by `convert_pdf_safe`'s chunk fallback. Their `doc_json` holds only the last chunk, numbered 1..N relative to that chunk (`_merge_chunk_results` keeps `chunks[-1]["document"]`). Their `metadata_json` is `{"degraded_modes": ["chunked"], "used_chunking": true}`. `"chunked"` is not in `DOCLING_QUALITY_LOSS_MODES`.
- **Unguarded structured path.** `BulkExtractor._docling_representation(pdf_path, page_range)` hands the doc_json to `representation_from_docling(page_range=…)` without checking that page numbers are PDF-absolute. The legacy `_slice_docling_text` has that check; the structured path does not.
- **Wrong-page structure.** A span that overlaps 1..N gets structure from the wrong pages. Example: artifact 1661, request pages 1–3 → Leaf 600 redline tables from the end of a 106-page filing.
- **Empty structure with no fallback.** A span beyond N gets an empty `DocumentRepresentation`. That object is not `None`, so `_structured_representation` returns it and **skips the native fallback** in hybrid mode.
- **Exposure:**

  | Bound NC documents | Count | With a tariff version |
  |---|---:|---:|
  | Total | 229 | 175 |
  | Spans overlap the chunk-relative numbers (wrong structure) | 44 | 41 |
  | Spans lie beyond the chunk (empty structure) | 183 | 134 |
  | Unbounded | 2 | 2 |

  Four benchmark documents are affected (hd 7378, 3140, 7365, 0378). The benchmark does not see this because it reads `bench/docling/*.docling.json.gz`.

## Scope

**In:**
1. A coverage check for Docling doc_json against the PDF on disk.
2. Use it in the structured path only.
3. Hybrid falls back to native when the Docling slice is unusable or empty.
4. The fallback reason is recorded in candidate runs.
5. A read-only exposure audit.
6. A benchmark option that reads Docling artifacts from a (scratch) DB.

**Out:**
- Changing `_merge_chunk_results` / `convert_pdf_safe` (CRITICAL; replaced in M6).
- Changing `docling_artifact_is_degraded` or `_slice_docling_text` (both CRITICAL per GitNexus; legacy text output must stay byte-identical).
- Any production write, migration, reconversion or promotion.
- Docling upgrade.

## Source modules

| File | Change |
|---|---|
| `src/duke_rates/db/artifact_cache.py` | **New** `docling_page_coverage(doc_json: dict, *, pdf_page_count: int, metadata: dict \| str \| None) -> dict`. Returns `{"pages": sorted page_nos with content, "max_page": int, "pdf_page_count": int, "chunked": bool, "absolute": bool, "complete": bool}`. `absolute` is False when `used_chunking`/`chunked` is set and the doc_json page count is below `pdf_page_count`. `complete` = `absolute` and the doc_json covers all pages. Pure function, no DB access. |
| `src/duke_rates/historical/ncuc/pipeline/bulk_extractor.py` | `_docling_representation`: after `json.loads`, call `docling_page_coverage` (PDF page count via the existing PyMuPDF open or `bounded.page_count`; memoize per path+size+mtime like `_current_file_hash`). If not `absolute`, or the requested `page_range` is not fully inside `pages`, return `None` and store the reason on `self._last_structured_fallback` (or return a small `(rep, reason)` from a new private helper; keep the public signature). `_structured_representation`: in `hybrid`, treat a representation with zero pages in range as unusable (fall through to bounded if enabled, else native). `docling` mode keeps returning `None` (no structure) rather than silently switching backend. Record `structured_input["docling_fallback_reason"]` (`chunk_relative_artifact`, `span_not_covered`, `empty_slice`) in `selection_details`. |
| `src/duke_rates/historical/ncuc/pipeline/candidate_runs.py` | When `structured_input.docling_fallback_reason` is set, add an `info` finding `docling_artifact_unusable` (not `review`; the native fallback is complete for its own backend). |
| `src/duke_rates/cli_commands/` (provenance or doc_intel sub-app, whichever already hosts read-only audits) | **New** `audit-docling-coverage --db PATH [--json OUT]`. Read-only (`mode=ro`). Lists every artifact with `absolute=False` or incomplete coverage and the bound `historical_documents` / `tariff_versions` with classification `wrong_pages` / `not_covered` / `unbounded`. Must reproduce 107 / 44 / 183 / 2 on the current production DB. |
| `bench/run_extraction.py` | **New** `--docling-source {cache,db}` (default `cache`, behavior unchanged). `db` uses the extractor's own `_structured_representation` against the read-only scratch DB, so production artifact selection is exercised. |
| `tests/` | See below. |

## Pre-work (mandatory, per CLAUDE.md)

1. Run `npx gitnexus analyze`; the index is 23 commits stale. **Trap:** `analyze` rewrites `CLAUDE.md`, so run `git diff CLAUDE.md` afterwards and restore it if it changed.
2. Run `gitnexus_impact` (upstream) on `_docling_representation`, `_structured_representation`, `BulkExtractor.extract_charges_from_document`, `record_candidate_run` (or the candidate-findings function actually touched), and `main` in `bench/run_extraction.py`. Report the risk level. If HIGH or CRITICAL, keep the change additive and get the user's go-ahead before editing.
3. Copy production to a scratch DB (`cp data/db/duke_rates.db $SP/m1.db`); never open production read-write. Record the production SHA-256 before and after.

## Regression tests

| Test file | Cases |
|---|---|
| `tests/test_artifact_cache.py` (extend) | `docling_page_coverage`: full doc → absolute and complete. Chunked metadata with 6 of 106 pages → `absolute=False`. Legacy row without metadata covering all pages → absolute. Doc with pages missing in the middle (`PARTIAL_SUCCESS`, skipped pages) → absolute but `complete=False`, with exact `pages`. |
| `tests/test_docling_artifact_selection.py` (extend) | A fake artifact row reproducing artifact 1661's shape (chunked metadata, pages 1..6 holding "Leaf No. 600" text; PDF fixture with ≥ 10 pages): `_docling_representation(path, (1,3))` → `None` with reason `chunk_relative_artifact`. Complete artifact → representation with original page numbers (unchanged behavior). |
| `tests/test_structured_extraction.py` (extend) | Hybrid with an empty Docling slice → the native representation is used. Docling mode → `None`, no native substitution. `selection_details.structured_input.docling_fallback_reason` set. Candidate run gets `docling_artifact_unusable` (info) and **no** `degraded_structured_input` review. |
| `tests/test_bounded_extraction.py` | Unchanged; must pass. With `bounded_conversion=True`, an unusable artifact routes to `bounded_representation`. |
| New `tests/test_audit_docling_coverage.py` | Audit on a synthetic DB: correct `wrong_pages` / `not_covered` / `unbounded` classification; opens the DB read-only (assert that a write raises). |
| Whole suite | `pytest -q` → 1681 passed / 92 skipped / 4 xfailed baseline (Phase 2D) plus the new tests, 0 failed. |

## Benchmark cases

1. **Committed cache (gate: identical).** `bench/run_extraction.py --mode {legacy,docling,native,hybrid}` on the scratch DB, then `diff_runs.py` against `reports/phase2c/bench_*.json`: field-identical in all four modes.
2. **Production artifacts (new).** `--mode hybrid --docling-source db` and `--mode docling --docling-source db` on the scratch copy. Report per document: backend actually used, fallback reason, charges, and evidence pages.
   - Required for hd 7378, 3140, 7365, 0378: no evidence page outside `[start_page, end_page]`, and no table-only rows sourced from a page outside the span.
   - Hybrid recall must be ≥ the committed-cache native-mode recall for those four documents.
3. **Contamination probe.** Run hybrid extraction (extract only, no persistence, in a scratch script) for the 41 `wrong_pages` versions, before and after the change. Before: count charges whose evidence page lies outside the span or whose table id comes from the truncated artifact. After: must be 0. Store the results in `reports/phase2e_m1/contamination_before_after.json`.
4. **Billing gate.** `bench/billing_check.py` / frozen 12-bill test on the scratch copy: unchanged (2 passed).

## Acceptance criteria

- [ ] `audit-docling-coverage` reproduces 107 PDFs and 44 / 183 / 2 bound documents on the production DB, read-only.
- [ ] Zero structured evidence outside the bound span on the 229 exposed documents (scratch extraction).
- [ ] The 134 `not_covered` versions get a native representation in hybrid mode (`backend=native`).
- [ ] Committed-cache benchmark field-identical in all four modes. Legacy text path byte-identical (no change to `_slice_docling_text`).
- [ ] Full suite green. 12-bill reconciliation unchanged.
- [ ] `gitnexus_detect_changes` shows only the intended symbols. Production SHA-256 unchanged.
- [ ] Report `docs/reports/2026-10-2x_phase2e_m1_implementation.md` with the numbers above.

## Safety constraints

- No writes to `data/db/duke_rates.db`. All extraction runs use a scratch copy. The audit opens production with `mode=ro`.
- No `provenance migrate`, no promotion, no reconversion, no new model downloads, no env changes (`.venv312` only).
- Do not edit `docling_artifact_is_degraded`, `_slice_docling_text`, `_merge_chunk_results`, `convert_pdf_safe` or `convert_pdf_with_docling`.
- Do not push or merge. Commit in logical steps (helper + tests; extractor + tests; audit; bench option; report).

## Implementation sequence

1. Pre-work: re-index (check `CLAUDE.md`), run impact, make the scratch copy, record the production SHA.
2. `docling_page_coverage` + unit tests (commit).
3. Run the contamination probe **before** the change and save the baseline.
4. Extractor guard + hybrid native fallback + `selection_details` reason + candidate finding + tests (commit).
5. `audit-docling-coverage` CLI + test; run it read-only on production and save the output (commit).
6. `--docling-source db` in `bench/run_extraction.py` (commit).
7. Benchmarks 1–4, the contamination probe after, the full suite, the billing gate.
8. `gitnexus_detect_changes`, production SHA check, write the report (commit).

**Expected size:** about 150–250 lines of source plus tests. One session.
