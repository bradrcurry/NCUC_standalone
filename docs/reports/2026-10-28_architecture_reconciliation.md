# Phase 2E: Repository Consolidation and Document Intelligence Architecture Reconciliation

**Date:** 2026-10-28
**Branch:** `phase2e/document-intelligence-reconciliation`, cut from `phase2c/unified-extraction` at `34a8cd5` (Phase 2C HEAD `9db5f13` plus the four Phase 2D commits).
**Method:** Repository inspection, read-only SQL against production (`data/db/duke_rates.db`, opened with `mode=ro`), and one read-only adapter run on a cached artifact. No source code changed. No migration, ingestion, reprocessing or promotion. The validated `.venv312` was used as is.
**Evidence labels:** **[Confirmed]** = reproduced from code and/or measured in this session; **[Likely]** = code path confirmed, impact not measured; **[Unverified]** = assumption or inference.

The companion execution plan for the next milestone is `docs/reports/2026-10-28_phase2e_m1_execution_plan.md`.

---

## 1. Executive summary

1. **Phase 2D is preserved** in four commits (`c1b1d6b`, `b7e2b3e`, `e4881f2`, `34a8cd5`). A new branch `phase2e/document-intelligence-reconciliation` sits on top. Nothing was pushed or merged. The production DB was not written; its mtime is still 2026-10-26 12:36.

2. **New, confirmed defect: chunked Docling artifacts make the structured path read the wrong pages.**
   - All **107** `docling_artifacts` rows with status `success` (lowercase; written by `convert_pdf_safe`'s chunk fallback) store a `doc_json` holding **only the last chunk**. Its page numbers are chunk-relative (1..N). Example: a 316-page compliance book whose stored document has 6 pages.
   - The cause is `docling_backend._merge_chunk_results`. It concatenates text and tables but keeps `chunks[-1]["document"]`. Phase 2C reported this as a limitation for `process-docling-batch` only.
   - The legacy text slicer (`BulkExtractor._slice_docling_text`) guards against chunk-relative artifacts. The Phase 2B/2C structured path (`_docling_representation` → `representation_from_docling(page_range=…)`) **does not**.
   - Reproduced on artifact 1661, an e-2-sub-1300-sept2023 bundle. Page 1 of the filing is an *Index of Tariffs*. Asking for pages 1–3 returns pages labeled 1–3 that contain **Leaf No. 600 redline tables from the end of the filing**, plus 6 tables.
   - Exposure: **229 NC historical documents** (175 with tariff versions) are bound to these 107 PDFs.
     - 44 docs (41 versions) have spans that overlap the chunk-relative numbers. They would get **wrong-page structure**. FCAR, leaf-501, leaf-528, leaf-600, RS, ES and others are affected.
     - 183 docs (134 versions) have spans beyond the chunk. They get an **empty** representation. In hybrid mode an empty (not `None`) representation **suppresses the native-geometry fallback**.
   - **Four of the 26 benchmark documents sit on such PDFs** (hd 7378, 3140, 7365, 0378). The benchmark hides this because it reads its own committed Docling cache, not production artifacts.
   - This must be fixed before the Phase 2C plan of hybrid candidate runs on production. **[Confirmed]**

3. **Segmentation works for real compliance books but has no notion of "not a tariff" for large non-tariff filings.**
   - On the 316-page e-2-sub-1300-sept2023 book (102 distinct leaves), `segment_document` produces 328 spans, 326 of them leaf-anchored. `document_sections` types it well (rate_schedule 0.80, rider 0.73).
   - On a 598-page Public Staff appeal record (e-2-sub-1300 `32d25655`), it produces **3 spans, all `doc_type='tariff'`**. Their "schedule titles" are prose sentences. `document_sections` labels 166 of its pages `rate_schedule`.
   - On a 1,097-page DEC DSM/EE filing (e-7-sub-1285 `543b80be`), all 19 spans are `tariff`. Only pages ≈1060–1073 are a tariff leaf (Rider EE, leaf 62). **[Confirmed]**

4. **Wasted work.**
   - Both of those large filings have a native text layer (sampled pages: 500–2,900 characters). Yet every page of both was mined through OCR (`artifact_source='ocr'`). **[Confirmed for sampled pages; whether OCR was needed on other pages is Unverified]**
   - The page cache is keyed by path. **30,057** page-artifact rows duplicate the same (file hash, page) under another path.
   - Run metadata still embeds full document text: `parse_attempt_logs` holds 1.50 GB, and 25,527 of its rows contain `full_text`. The Phase 2C candidate path still writes it (`text_metrics["full_text"]` → `record_parse_attempt`). **[Confirmed]**

5. **Reuse, not rebuild.** The infrastructure for page indexing, segmentation, hierarchy and representation caching already exists. It is either partly wired or unused:
   - `ncuc_page_artifacts` / `ncuc_span_artifacts` (versioned caches);
   - `document_sections` with `parent_section_index` (1,075 rows already set; nothing consumes it);
   - `section_type_gold` (964 rows) with section embeddings (28,718 vectors);
   - `RepresentationCache` (content-addressed gzip; **no production caller**);
   - `bounded.py` (page-correct chunking).

   The roadmap below connects these pieces rather than adding a framework.

6. **Docling upgrade order:**
   - First, guard structured input against bad artifacts (M1).
   - Then make the layout model and revision part of the backend/cache key (Phase 2D Stage 1).
   - Only after that, promote 2.143 + Egret-medium and reconvert, through `bounded.py` rather than `convert_pdf_safe`'s chunk merge. Otherwise reconversion re-creates truncated artifacts.

   Segmentation work (M4/M5) does not depend on the Docling version. It can run in parallel after M2 (the boundary ground truth).

7. **Next milestone (M1, "structured-input integrity"):**
   - A page-coverage guard for Docling artifacts.
   - A native fallback when the Docling slice is empty or unreliable.
   - A read-only exposure audit.
   - A benchmark option that reads production-style artifacts.

   It is small and touches only Phase 2B/2C code paths. It leaves the two GitNexus-CRITICAL functions (`docling_artifact_is_degraded`, `_slice_docling_text`) unchanged.

---

## 2. Git and worktree consolidation

| Item | State |
|---|---|
| Primary checkout | `C:\Python\Duke\Standalone`, confirmed as the development workspace |
| Branch before | `phase2c/unified-extraction` @ `9db5f13` (28 commits ahead of `main` @ `d6a0cb3`, merge base = `main`) |
| Branch now | `phase2e/document-intelligence-reconciliation` @ `34a8cd5` + this phase's docs commit |
| Phase branches | `phase2a/provenance-billing-safety` `2cee58e`, `phase2b/structured-extraction` `438c7f8`, `phase2c/unified-extraction` `34a8cd5` (Phase 2D commits added here), `stabilize/phase0-docling-rider-fixes` `25def84`. Linear history; none rewritten. All local only (no upstream). |
| Temporary worktree | `…/b85668a7-…/scratchpad/baseline_tree` @ `8b77aa4` (detached, clean, 15 MB). **Referenced only as a recipe** in `bench/README.md` (`git worktree add --detach $SP/baseline_tree 8b77aa4`) and as a recorded `src` path in `reports/phase0/bench_baseline.json`. No test or script needs it to exist. It can be removed with `git worktree remove` and re-created from `8b77aa4` at any time. **Left in place**, as instructed. |
| Phase 2D scratch data | The Phase 2D conversion caches (8.8 MB) and excerpts (8.5 MB) are still in the previous session's scratchpad (`…/68255879-…/scratchpad/{conv,excerpts}`). They are reproducible with `bench/phase2d/make_excerpts.py` and `convert_corpus.py`, so they were not committed. Copy them before that temp directory is cleaned if Stage 2 should reuse them. |

**Untracked files left untouched (not Phase 2D):**

| Path | What it is | Decision |
|---|---|---|
| `fake.db` | 0-byte file, 2026-05-31 | Not committed. Looks like a test leftover. Delete only on approval. |
| `reports/_*.txt` (34), `reports/_recovered_paths.json` | 2026-05-31 / 06-05 completeness-audit outputs (memory: `session_completeness_audit_2026_05_31`) | Not committed; purpose known, value historical |
| `scripts/_*.py` (17) | Read-only investigation scripts from the same audit | Not committed. Consider moving useful ones (`_completeness_audit.py`, `_resi_completeness.py`) into `scripts/diagnostics/` in a later cleanup. |
| `reports/phase2d/env/release_notes_raw.json` | 600 KB raw GitHub release dump (Phase 2D) | Not committed; reproducible. The filtered list is committed. |

## 3. Phase 2D preservation

| Commit | Content |
|---|---|
| `c1b1d6b` bench(phase2d) | `bench/phase2d/{make_excerpts,convert_corpus,compare_conversions,diff_runs}.py`; `--docling-cache` option in `bench/run_extraction.py` (default unchanged); `.gitignore` entries for `.venv-docling-next/`, `.cache-docling-next/` |
| `b7e2b3e` reports(phase2d) env | Freezes for `.venv312`, `.venv312-ocr`, `.venv-docling-next`; target and relaxed pins; PyPI metadata; filtered release notes |
| `e4881f2` reports(phase2d) measurements | 26-document runs and scores per environment and variant (`bench/`, `bench_ocr/`), per-run RSS/VRAM/CPU metrics and logs (`runs/`), conversion/OCR comparisons, resource summary, full-suite outputs, production SHA before/after (about 16 MB, in line with the Phase 2C report sizes) |
| `34a8cd5` docs(phase2d) | `docs/reports/2026-10-27_dependency_upgrade_benchmark.md` (force-added because `docs/reports/` is ignored, as for earlier phases; header updated to say it is committed) |

- **Secret review.** A pattern scan (HF/OpenAI-style tokens, `Bearer`, `api_key`, `password`, `secret`) over all committed Phase 2D files hit only package names (`tokenizers`, `tiktoken`) and release-note prose ("password-protected PDF").
- No DB files, model weights, or caches were committed. `gitnexus detect_changes` before committing: 2 symbols touched, both in `bench/run_extraction.py`; risk low; no execution flows affected.

## 4. Current end-to-end architecture (as the code runs today)

```mermaid
flowchart TD
  subgraph Acquire["Discovery / download"]
    D1[NCUC portal + Duke site crawl<br/>historical/ncuc/session.py, discovery/*] --> D2[(PDF on disk<br/>path-keyed; same bytes often under 2–3 paths)]
  end

  subgraph Ingest["Ingest — historical/ncuc/importer.py (_process_record)"]
    D2 --> T[triage_pdf  pipeline/triage.py<br/>re-hashes + samples every run; NOT cached]
    T -->|native| PM[mine_document_pages  page_miner.py<br/>pdfplumber, regex features]
    T -->|OCR_REQUIRED| OCR[extract_pages_with_progressive_ocr  ocr.py]
    PM & OCR --> PA[(ncuc_page_artifacts<br/>key: source_pdf+file_hash+version<br/>page_miner_v6: 151k rows; 30k dup rows<br/>redline/TOC signals NOT persisted)]
    PA --> SEG[segment_document  segmentation.py<br/>flat TariffSpan list; confidence unscaled 0–88]
    SEG --> SA[(ncuc_span_artifacts segmentation_v8)]
    SA --> FAM[family match → historical_documents start/end_page → tariff_versions]
  end

  subgraph Side["Parallel / side segmenters"]
    PA & SA --> SECT[section_aggregator.py → document_sections<br/>types + parent_section_index<br/>consumed only by doc-intel gold/embeddings]
    LS[parse/leaf_splitter.py → ingest_pipeline<br/>→ ncuc_ingest_segments 4,490 rows] 
    KNN[EmbeddingKNN / SectionKNN<br/>qwen3-embedding, bge-m3]
  end

  subgraph Docling["Docling lane — docling_backend.py (batch CLI)"]
    D2 --> DC[convert_pdf_with_docling / convert_pdf_safe<br/>new converter per call; Heron + TableFormer v1]
    DC -->|chunk fallback| MCR[_merge_chunk_results<br/>keeps LAST chunk's document only]
    DC & MCR --> DA[(docling_artifacts 1,964 rows / 459 MB doc_json<br/>107 'success' rows truncated)]
    DA --> DPM[docling_page_miner → page artifacts docling_page_miner_v1]
  end

  subgraph Extract["Extraction — bulk_extractor.py (BulkExtractor.extract_document)"]
    FAM --> TXT[extract_text_from_pdf<br/>Docling slice (guarded) else pdfplumber]
    TXT --> GATE[classify_document + skip rules<br/>re-opens PDF: _find_tariff_type_in_pages, classify_compliance_book]
    GATE --> PROF[HistoricalRateParserRegistry 60 profile classes<br/>4 consume structured tables]
    FAM -.mode≠legacy.-> REP[_structured_representation<br/>Docling artifact (UNGUARDED page_range) → else bounded.py → else native]
    REP --> PROF
    PROF --> VAL[charge_validators + hybrid merge<br/>TOTAL reconciliation]
    VAL --> CR[(extraction_runs candidates — Phase 2A/2C schema<br/>production NOT migrated)]
    PROF --> LOGS[(parse_attempt_logs 1.50 GB / processing_runs 1.12 GB<br/>full_text still embedded)]
  end

  CR --> PROMO[provenance compare / promote / rollback] --> TC[(tariff_charges 5,198 rows)]
  RS[(rider_summary_blocks / line_items v2 runs)] --> TL[residential timeline / analytics]
  TC --> BILL[billing engine, 12-bill reconciliation]
```

## 5. Capability inventory by stage

| Stage | Implemented and used | Partial / unused / duplicated | Structure or provenance lost | Reprocessing / inefficiency | Classification-driven loss |
|---|---|---|---|---|---|
| Discovery / archive | Portal + site crawl, `document_blobs` content hash (Phase 2A, scratch only) | Production has no `document_blobs` (not migrated) | Path is the identity in all artifact tables | Same bytes stored under several dockets/paths (e.g. `9113a9ae…` in 3 paths) | — |
| Triage (`triage.py`) | Scanned/native, archetype, table-mode hints | `table_mode_candidate`, `reading_order_risk` recorded but barely consumed | — | **Runs on every ingest before the page cache is checked** (full-file SHA + sampled text) **[Confirmed]** | Routes large native-text filings to OCR **[Confirmed on 2 samples]** |
| Page mining | `page_miner_v6` regex features; Docling page miner (D1 table-cell fix) | `PageEvidence` redline/TOC/dual-rate fields **not persisted** by `save_page_artifacts` and lost on reload **[Confirmed]** | Only the first/last 5 lines as header/footer; no bboxes | Cache keyed by path: 30,057 duplicate rows; `_find_tariff_type_in_pages` and `classify_compliance_book` **re-open the PDF** with pdfplumber at extraction time **[Confirmed]** | — |
| Segmentation (`segmentation.py`) | Leaf-header/title boundaries; merge pass | `TariffSpan.confidence` is not a probability (0–88); `dates` unused; three other segmenters (leaf_splitter, section_aggregator, Docling slicer) | Flat list; no parent/child; no boundary evidence per page | Re-segments only when the version changes (good) | `_classify_page_doc_type` labels prose `tariff` (§7.2) |
| Classification | `BulkExtractor.classify_document`, skip categories, KNN, section gold | Doc type decided up to 5 times (triage, segmentation, section aggregator, extractor, KNN) with no shared result | — | Extractor reclassifies from text on every run | 6–8 rider sheets misrouted as `order` (audit §6, unchanged: production untouched since) |
| Family / profile selection | 60 profile classes, `_score_profile`, fallbacks with cross-attribution guard | — | — | — | 24 `unknown`, 30 empty-profile (audit counts; production coverage still 376/526) |
| Structured extraction | `DocumentRepresentation`, `table_api`, Docling/native/OCR adapters, `bounded.py` | `RepresentationCache` has **no production caller**; bounded output is not cached; only 4 profile classes (JAA, single-value rider, two adjustment matrices) consume tables | OCR adapter has no bboxes/tables; Docling artifacts may be chunk-relative (§1.2) | `_docling_representation` `json.loads` the whole `doc_json` (up to 4.4 MB) for each version bound to a bundle | Unguarded chunk-relative artifacts → wrong or empty structure (§1.2) |
| Validation | Hybrid merge, TOTAL reconciliation, six conflict validators, review gate | Line effective dates captured, not scored | — | — | — |
| Candidate storage | Immutable runs, evidence, promote/rollback (scratch) | Production not migrated (deliberate) | — | Run logs still embed `full_text` | — |
| Billing / history | 12-bill frozen baseline, canonical units | — | — | — | — |

## 6. Duplicated and unused infrastructure

| Function | Implementations | Recommendation |
|---|---|---|
| Logical segmentation | `segmentation.segment_document` (→ span artifacts, drives binding); `parse/leaf_splitter.split_pdf_into_leaves` (→ `ncuc_ingest_segments`); `section_aggregator` (→ `document_sections`, refines spans); `BulkExtractor._slice_docling_text` / `text_slicer` (text slicing) | Keep `segment_document` as the boundary producer and `section_aggregator` as the typing/hierarchy layer on top of it. Freeze `leaf_splitter` (do not extend). |
| Docling → pages | `docling_page_miner.mine_pages_from_docling_artifact` (PageEvidence) and `adapters.representation_from_docling` (DocumentRepresentation) | Long term, derive PageEvidence from a DocumentRepresentation; do not add a third. |
| Native tables | `native_tables.py` (pdfplumber/camelot, used by the normalization router) and `adapters._geometry_tables` (word geometry, used by extraction) | Keep the adapters version for extraction; leave the router. |
| Chunked conversion | `convert_pdf_safe` chunking (`_merge_chunk_results`, lossy) and `bounded.py` (page-correct, degraded-aware) | Route batch conversion through `bounded.py` (M6) instead of fixing the CRITICAL merge in place. |
| Doc-type classification | triage, `_classify_page_doc_type`, `section_aggregator` types, `classify_document`, KNN | One persisted per-section decision (M5). |
| Unused or partly wired | `RepresentationCache`; `document_sections.parent_section_index` (1,075 set, unused); `DocumentNormalizationRouter` + Paddle/GLM normalizers (service/bench only); `TariffSpan.dates`; `ncuc_ingest_segments` | Wire the cache (M6) and the hierarchy (M5); leave the rest. |

## 7. Large and bundled NCUC documents

### 7.1 Archive profile (page artifacts, `page_miner_v6`)

| Pages | PDFs | Pages total |
|---|---:|---:|
| < 10 | 3,222 | 11,705 |
| 10–49 | 986 | 20,537 |
| 50–99 | 204 | 14,939 |
| 100–199 | 137 | 19,907 |
| 200–299 | 34 | 8,315 |
| 300–499 | 18 | 6,845 |
| 500+ | 11 | 7,623 |

- The 200 PDFs of 100+ pages hold **47 % of all mined pages**.
- The largest is e-2-sub-1174 `e7c07401` at 1,399 pages. Its Docling run was `PARTIAL_SUCCESS`, with 48 tables over 1,399 pages. It binds one version (leaf-707, pages 29–35).

### 7.2 Segmentation on representative filings **[Confirmed, read-only]**

| Filing | Pages | Text source | Spans (v8) | Span doc_type | `document_sections` | Assessment |
|---|---:|---|---|---|---|---|
| e-2-sub-1300-sept2023 `3f92be50` (DEP compliance book) | 316 | native | 328 (108 one-page; 326 leaf-anchored); 102 distinct leaves | all tariff | rate_schedule 63 (0.80), rider 27 (0.73), T&C 16, unknown 4 | **Good.** Leaf-header segmentation fits compliance books. 63 hd docs / 42 versions bound. |
| e-2-sub-1207 `32b7c724` (hd 5324 JAA) | 110 | — | 88 over 121 pages (path variants) | 80 tariff | — | Bench span 17–31 OK. Docling artifact `PARTIAL_SUCCESS` (110 pages, 38 tables). |
| e-2-sub-1300 `32d25655` (Public Staff appeal record) | 598 | **OCR** (has a text layer) | **3** (1–254, 255–393, 394–598) | **all tariff** | rate_schedule 166 pp, procedural 218 pp | **Wrong type.** No leaf headers. Prose lines pass `SCHEDULE_HEADING_REGEX`, and the procedural vocabulary density stays low on legal prose. Harmless today because no version binds to it, but it would produce false tariff candidates if discovery auto-bound spans. |
| e-7-sub-1285 `543b80be` (DEC DSM/EE filing) | 1,097 | **OCR** (has a text layer) | 19 | all tariff | rate_schedule 849 pp | Only ≈1060–1073 is tariff (Rider EE leaf 62). Evaluation reports are typed rate_schedule. |

**Conclusions.**
- Boundary detection is adequate when leaf headers exist. The failure is **type assignment and the absence of a "non-tariff" region concept**.
- **No hierarchy.** A filing → logical document (application, testimony, exhibit, compliance tariffs) → leaf → page chain is not represented. `document_sections.parent_section_index` exists and is partly populated, so the column for it is already there.
- **No boundary confidence.** Spans carry an unscaled score, and there is no per-boundary evidence (which signal fired).
- **Redline vs approved** is decided per document (`is_redline`), not per span or page. The redline page signals are dropped from the cache (§5).

### 7.3 Segmentation intelligence (what to combine, in this order)

1. **Deterministic signals already computed per page:** leaf number transitions, revised/original header, schedule title, effective-date phrase, docket phrase, and running header/footer text (first/last lines). Add a header/footer *discontinuity* feature, the Jaccard of normalized header lines between adjacent pages, computed from the cached `header_candidates`. No new extraction is needed.
2. **Existing embeddings:** `section_embeddings` (bge-m3 and qwen3-embedding, 14,359 sections each) and `section_type_gold` (964 rows: 795 rate_schedule, 120 rider, 35 procedural, 14 T&C). The gold set is heavily rate-schedule-skewed. It needs procedural and exhibit negatives from the large filings above before it can veto `tariff`.
3. **Selective LLM/VLM adjudication** only for boundaries where (1) and (2) disagree or confidence is low. Send the two adjacent pages' text (and the page image for scans), never the filing. On the samples above, that is a handful of boundaries per 500-page filing.

### 7.4 Targeted extraction and resources

| Capability | Status |
|---|---|
| Span-only conversion | `bounded.py` converts only the span, in 8-page chunks, keeps original page numbers, falls back to native per chunk, and records `degraded`. Measured in Phase 2D: hd 5324 span 17–31 → 15 pages, 27 tables, 102 s (2.91) / 155 s (2.143). **[Confirmed]** |
| Docling `page_range` | Also page-correct; not faster in Phase 2D. |
| Chunk overlap and page ownership | **Missing.** Tables crossing a chunk boundary are split. Continuation tables are reconciled only inside the rider-summary parser (x-position mapping). |
| Partial-extraction reporting | `bounded.py` and candidate runs (`degraded_structured_input` finding): yes. `convert_pdf_safe` artifacts: `degraded_modes=["chunked"]`, but `chunked` is not a quality-loss mode, so consumers treat it as complete (§1.2). |
| Memory / GPU | One converter per call. Under 2.91 VRAM grows about 170 MB per conversion; under 2.143 it is flat. RSS/VRAM sampling and the low-memory watchdog exist only in bench tooling (`bench/phase2d/convert_corpus.py`, `bench/bounded_memory.py`). `bounded.peak_rss_mb` exists. There is **no worker-process isolation** in production conversion, so a docling-parse segfault (the Tesseract route on 2.91) kills the batch process. |

## 8. DocumentRepresentation as the shared representation

**Preserved today:**
- source path and SHA-256;
- original page numbers (`page_offset`);
- page text, markdown, width and height;
- blocks with type, bbox and reading order (Docling `body` walk), and furniture flags;
- tables with cells, spans and column-header flags;
- backend, backend version, representation version, and `coordinates_available`.

**Gaps (each a small additive field or convention, not a new contract):**

| Gap | Effect | Proposed extension |
|---|---|---|
| Source coverage: no record of which pages the producer actually covered | Truncated/chunk-relative artifacts look complete (§1.2) | `document_metadata["page_coverage"] = {"requested": [s,e], "covered": [...], "complete": bool}` set by every adapter; consumers reject `complete=False` for structured mode |
| Converter configuration identity | Layout model/revision not recorded; Phase 2D shows Heron vs Egret changes tables | `backend_version` → include layout model and revision (Stage 1); `RepresentationCache` options fingerprint already supports it |
| Row headings / multi-row headers | `table_api` infers header rows; cells carry `is_column_header` only | Add `is_row_header` (Docling provides `row_header`); no schema change, since cell `metadata` can carry it first |
| Rate-class and effective-date context | Derived by profiles per call; lost between stages | Leave in profiles; record on evidence (already done in Phase 2B) |
| Hierarchy (section → page) | Representation is page-flat | Do not add to the representation; keep hierarchy in `document_sections` and pass `page_range` |
| OCR structure | `representation_from_ocr_text` has no bboxes or tables | Fill from Docling RapidOCR output (Stage 5) rather than a new OCR adapter |
| Page features | `PageEvidence` is a separate model | Keep. Derive it from the representation later (§6), and do not merge the models now. |

**Verdict:** the representation is fit to be the shared contract for extraction, validation and provenance. It needs **coverage metadata (M1)** and **configuration identity (M3)**. It should not try to carry segmentation hierarchy.

## 9. Docling dependency adoption strategy

The Phase 2D findings stand. Recommended set: docling 2.143.x / docling-core 2.105 / docling-parse 7.23 / ibm-models 4.0.4, torch 2.14.1+cu130, **Egret-medium pinned `77ede7cc…`**, TableFormer v1 ACCURATE, OCR off for text-layer PDFs, RapidOCR auto only on the scanned route.

Ordering relative to the rest of the roadmap:

| Step | Precedes / follows | Gate |
|---|---|---|
| **M1 integrity guard** (this plan) | First. It is version-independent: the truncated artifacts exist under 2.91 and would be re-created by any chunked reconversion. | Benchmark identical on the committed cache; the production-artifact benchmark shows wrong-page structure eliminated |
| **Stage 1: layout/revision in config + cache key; optional converter reuse** | After M1, in the current env (Heron default) | Byte-identical artifacts with the Heron default; new key fields present; GitNexus impact on `convert_pdf_with_docling` (CRITICAL) reviewed |
| **Stage 2: rebuild bench cache with 2.143 + Egret** in `.venv-docling-next` | After Stage 1 | Docling recall ≥ 0.768, hybrid charges identical to 2.91, TOTAL ≥ 0.680, zero-doc FP 0, full suite green, live 12-bill reconciliation 2/2 |
| **Stage 3: promote env** (new venv from lockfile; keep `.venv312` for rollback) | After Stage 2 | Same as Stage 2 on the promoted venv; overnight `pwsh` scripts switched |
| **Stage 4: reconvert production artifacts** | After M6 (batch conversion through `bounded.py`), after `provenance migrate` approval | No chunk-relative artifacts written (coverage check); per-family candidate review |
| **Stage 5: scanned route** (RapidOCR + table-aware DEC RS) | After Stage 3 | hd 7380: ≥ 25/31 expected values become charges with evidence; no digital-doc change |

Segmentation (M4/M5) is independent of the Docling version, because it works on page artifacts. Run it after M2 provides boundary ground truth, in parallel with Stages 1–3.

## 10. Extraction accuracy and efficiency bottlenecks

1. **The gap set has not changed in production.** NC versions with charges are still **376 of 526**, and `tariff_charges` is still 5,198 rows. Phases 2A–2D wrote only scratch copies. The audit's ≈58 real gaps (24 no profile, 30 empty profile, 4 unclassified) and 6–8 misrouted `order` sheets remain the working list. Phase 2C hybrid gains (benchmark recall 0.7607 → 0.7732) have not been applied. **[Confirmed]**
2. **Structured input reaches only 4 of 60 profile classes:** JAA, single-value rider, and the Progress/Carolinas adjustment matrices. Rider summaries also go through `_rider_summary_with_structure`. Every other profile parses flattened text.
3. **Scanned DEC RS (hd 7380).**
   - `CarolinasResidentialFlatProfile.extract` calls `parse_nc_carolinas_leaf(text)` only, so RapidOCR's 29/31 cell recovery cannot reach it.
   - The cheapest fix is a `_class_rate_rows`-style structured branch for this profile, consuming `table_api.find_rate_tables` on a RapidOCR Docling representation. It needs no new model. It depends on Stage 3 (RapidOCR is broken or ineffective under 2.91).
4. **Wrong or empty structure on 229 bundle-bound documents** (§1.2). This is the largest *silent* accuracy risk for the planned hybrid production run.
5. **Classification gating.** Doc type is decided from text at extraction time. `document_sections` (which has better evidence for bundles) is not consulted. That is how real rider sheets end up `skipped_order`.
6. **Efficiency.**
   - Triage and file hashing are repeated on every ingest.
   - Full-filing OCR runs when a text layer exists.
   - The page cache is path-keyed (30k duplicate rows).
   - Run logs embed full text (1.5 GB + 1.1 GB).
   - The whole `doc_json` is parsed once per bound version (a 316-page bundle with 63 bound docs parses the same JSON 63 times per run).
   - Bounded conversions are not cached, so each candidate run reconverts.
7. **More charges is not the goal.** Contaminated structure would *add* plausible table-only rows when a contaminating section reconciles. M1's acceptance criterion is "no structure from outside the span", not charge count.

## 11. Expanded benchmarking proposal

Keep the 26-document extraction benchmark and the 12-bill baseline unchanged as regression gates. Add three layers.

**A. Production-artifact extraction mode (M1).** `bench/run_extraction.py --docling-source db --db <scratch copy>` reads Docling artifacts from the DB the way production does, instead of the committed cache. This is the only way the four corpus documents on truncated artifacts (hd 7378, 3140, 7365, 0378) exercise the real path.

**B. Segmentation gold (M2).** Boundaries come from hand review of rendered pages, never copied from `ncuc_span_artifacts` or `document_sections`. Stored as `bench/segmentation/<filing>.yaml` with `logical_documents: [{start, end, kind, leaf, schedule, redline, parent}]`.

| Filing | Pages | Why |
|---|---:|---|
| e-2-sub-1300-sept2023 `3f92be50` | 316 | Native multi-tariff compliance book, 102 leaves, real hierarchy (index → schedules → riders) |
| e-2-sub-1207 `32b7c724` (hd 5324) | 110 | Phase 2B/2D large-doc case; JAA multi-column |
| e-7-sub-1285 `543b80be` | 1,097 | Mostly non-tariff (DSM/EE evaluation) with an embedded rider leaf range; negative control and needle-in-haystack |
| e-2-sub-1300 `32d25655` | 598 | Pure non-tariff legal record; false-positive control |
| hd 7380 DEC RS scan + one pre-2000 scanned bundle (to be picked from the 18 PDFs with ≥ 300 pages and `artifact_source='ocr'`) | — | Scanned boundaries |

These boundaries are **not yet verified**. M2 is the transcription task. The page ranges quoted in §7.2 are hypotheses from page text, used only to choose the filings.

**C. Metrics** (scorer `bench/score_segmentation.py`, plus extensions to `score.py`):

| Layer | Metrics |
|---|---|
| Discovery | Logical-document recall/precision at IoU ≥ 0.9; boundary F1 at ±0 and ±1 page; missed tariff leaves; false tariff pages (pages typed tariff inside non-tariff gold regions) |
| Classification | Utility, family, schedule vs rider, proposed/redline vs approved, effective-date accuracy (per logical document) |
| Extraction | Existing recall/precision/unit/rate-class/TOTAL, plus line effective dates (Phase 2B leftover), evidence page within the gold span (new: catches contamination), multi-page table continuity (rows of a gold continuation table attributed to one section) |
| Efficiency | Seconds per page and per relevant tariff page; peak RSS; peak torch VRAM; GPU util; pages sent to Docling or OCR vs pages in tariff spans ("expensive-page ratio"); cache hit rate (page, span, representation); conversions avoided; recovery rate after chunk failure. Reuse `convert_corpus.py`'s sampler. |

## 12. Prioritized modernization roadmap

| # | Item (letter from the brief) | Reuse | Modules likely to change | New functionality | Benefit | Complexity / risk | Depends on | Acceptance gate |
|---|---|---|---|---|---|---|---|---|
| **M1** | Structured-input integrity (E, prerequisite to A) | `bounded.py`, adapters, `docling_artifact_is_degraded` (unchanged) | `bulk_extractor._docling_representation` / `_structured_representation`, `db/artifact_cache.py` (new helper), `bench/run_extraction.py`, new read-only audit command | Page-coverage check; native fallback on empty or unreliable Docling slice; exposure audit | Removes wrong-page structure on 41 versions and restores the native fallback for 134 | S / low. New code is Phase 2B/2C-only; CRITICAL functions untouched | — | §13 plan |
| **M2** | Segmentation ground truth (benchmark) | `bench/` conventions, page renderer | `bench/segmentation/*`, `bench/score_segmentation.py` | Gold YAML for 5 filings; scorer | Makes C/D measurable | M (human transcription) / none | — | Gold reviewed against rendered pages; scorer reproduces baseline numbers for current `segment_document` |
| **M3** | Docling Stage 1 (A, part 1) | `stage_versions`, `RepresentationCache.options_fingerprint` | `docling_backend._build_standard_converter`, `stage_versions.py` | Layout model/revision option; backend key includes it; optional per-process converter reuse | Lets 2.143 + Egret artifacts coexist safely; removes 2.91 VRAM growth | S–M / **CRITICAL** symbol, so re-index and run impact first | M1 | Heron default byte-identical; suite green |
| **M4** | Persistent page index (B, G) | `ncuc_page_artifacts`, `artifact_cache` | `artifact_cache.save/load_page_artifacts`, `importer._process_record`, `bulk_extractor` gating helpers | Persist all PageEvidence fields; look up by `file_hash` first (path second); cache triage in page-artifact metadata; extractor reads cached pages instead of re-opening PDFs | Fewer PDF opens and OCR runs; restores redline/TOC signals | M / medium (importer is hot path) | M2 for measuring | Cache hit rate ≥ 0.95 on re-ingest of the bench filings; identical spans |
| **M5** | Segmentation typing + hierarchy (C, D) | `section_aggregator`, `document_sections.parent_section_index`, section embeddings + gold | `segmentation._classify_page_doc_type`, `section_aggregator`, gold seeding | Non-tariff regions (procedural/exhibit/testimony); boundary evidence; normalized confidence; filing → logical doc → leaf parents | Prevents false tariff spans in large filings; enables bundle-aware gating | M / medium | M2, M4 | Boundary F1 and false-tariff-page metrics improve on M2 gold; 26-doc benchmark and bill baseline unchanged |
| **M6** | Batch conversion via `bounded.py` + representation cache (E, G) | `bounded.py`, `RepresentationCache` | `docling_backend.convert_pdf_safe` callers (`process-docling-batch`), `bulk_extractor._bounded_representation` | Span-scoped batch conversion; content-addressed gzip representations outside SQLite; small overlap for table continuity with page ownership | No new truncated artifacts; no repeat conversions; smaller DB | M / medium-high (batch CLI) | M1, M3 | Zero artifacts with `page_coverage.complete=False`; conversion count per candidate run = 0 on cache hit |
| **M7** | Docling Stages 2–4 (A, part 2) | Phase 2D lockfile and tooling | env, bench cache | Promote 2.143 + Egret; reconvert family by family | Recall +3 pts in Docling mode, provenance 0.985, whole-doc reliability | M / medium | M3, M6, `provenance migrate` approval | Phase 2D gates (§9) |
| **M8** | Extend structured profiles (F) | `table_api`, `class_rate_tables` | DEC RS flat/TOU, leaf-500 TOU/tiered, DEC multi-class profiles | Structured branches like `_class_rate_rows` | Scanned DEC RS (Stage 5), fewer regex special cases | M per group / medium | M7 for OCR | Per-group benchmark recall ≥ legacy, precision ≥ 0.98 |
| **M9** | Selective AI adjudication (H) | `OllamaOrchestrator`, KNN | `section_aggregator` (hook) | LLM/VLM on low-confidence boundaries only (two adjacent pages) | Fixes the residual boundary errors M5 cannot | S–M / low (advisory, reviewed) | M5 | ≥ 50 % of remaining boundary errors fixed on M2 gold with ≤ 5 calls per 100 pages |
| **M10** | MinerU / PaddleOCR-VL / local VLMs (I) | Adapter pattern, Phase 2D harness | new adapter behind `DocumentRepresentation` | Bake-off only | Only for named failures: pre-2000 scans where RapidOCR < 80 % cell recovery; tables Egret misses | M / low (isolated) | M7, M2 | Beats 2.143 + Egret on the failing subset with no regressions elsewhere |

Deliberately **not** proposed: a new segmentation framework, a new page index table, a second representation model, or new models without a named failure.

## 13. Next implementation milestone

**M1: Structured-input integrity for production Docling artifacts.** The full plan, ready to hand to an implementation session, is in `docs/reports/2026-10-28_phase2e_m1_execution_plan.md`.

In short:
- add a page-coverage helper;
- make `_docling_representation` reject artifacts that do not cover the requested span with PDF-absolute page numbers;
- make hybrid fall back to native when the Docling slice is empty;
- record the fallback reason in `structured_input`;
- add a read-only exposure audit;
- add `--docling-source db` to the benchmark.

Acceptance: the committed-cache benchmark is identical, the production-artifact benchmark shows no structure from outside the span, the suite is green, and the 12-bill baseline is unchanged.

## Appendix A: read-only queries used

Scripts were run from the session scratchpad against `file:data/db/duke_rates.db?mode=ro`. Key figures:
- `parse_attempt_logs.metadata_json`: 64,064 rows, 1,497.7 MB, of which 25,527 contain `"full_text"`.
- `historical_processing_runs.metadata_json`: 41,179 rows, 1,119.0 MB.
- `ncuc_page_artifacts`: 174,199 rows; text 310.6 MB plus metadata 215.3 MB.
- `docling_artifacts`: 1,964 rows; `doc_json` 459.1 MB, of which 86.0 MB duplicates the same file hash.
- `ncuc_span_artifacts` `segmentation_v8`: 14,651 spans over 4,608 PDFs.
- `document_sections`: 14,360 rows over 3,319 PDFs.
- `section_type_gold`: 964 rows.
- NC versions with charges: 376 of 526.

Truncated-artifact exposure: 107 PDFs. Bound NC docs: 44 overlap the chunk-relative numbers (41 with versions), 183 lie beyond the chunk (134 with versions), and 2 are unbounded.
