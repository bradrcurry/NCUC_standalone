# Phase 2D: PDF Parsing Dependency Upgrade and Performance Benchmark

**Date:** 2026-10-27
**Branch:** `phase2c/unified-extraction` (committed 2026-10-28 during Phase 2E consolidation; `reports/phase2d/env/release_notes_raw.json` — 600 KB raw GitHub release dump — is kept locally but not committed; the filtered list is)
**Method:** A new, separate environment `.venv-docling-next` with its own model cache (`.cache-docling-next/hf`) was benchmarked against the validated `.venv312`.
- `.venv312`, `.venv312-ocr`, the user model cache (`~/.cache/huggingface`) and CUDA setup were not modified.
- No application code (`src/`, `tests/`) was changed. Only benchmark tooling was added (`bench/phase2d/`, and a `--docling-cache` option in `bench/run_extraction.py`).
- No candidate run was recorded or promoted. Production was hashed before and after and is unchanged (§10).
- No new parsing engine was introduced.

---

## 0. Answers

| Question | Answer |
|---|---|
| Does newer Docling improve tariff extraction accuracy? | **Not with the current configuration.** With the app's settings (Heron layout, TableFormer v1 ACCURATE, OCR off), 2.143 is *worse* in pure Docling mode: recall 0.7356 → 0.7168, TOTAL reconciliation 0.641 → 0.577. Hybrid charges are identical. **With the Egret-medium layout model it is better:** Docling recall 0.7682 (above legacy's 0.7607), TOTAL 0.680, precision 0.9808. Hybrid charges are still identical, and evidence coverage rises from 0.952 to 0.984. |
| Does it recover previously missing tables or values? | **Yes, through the layout model, not the version.** Egret-medium recovers DEC blk304's two missing tables (+18 charges, 64/64 values in cells) and blk361 (+8 charges). The same gain appears with Egret under 2.91. With Heron, 2.143 *loses* two tables on DEP leaf-600 2026-06-01. For the scanned DEC RS, 2.143's RapidOCR raises values found in Docling cells from 13/31 to 29/31. The current profile does not use them, so charges are unchanged. |
| Does it process large NCUC filings more efficiently? | **More reliably; speed is roughly the same.** The 110-page hd 5324 converts completely as a whole document under 2.143 (792 s, 3.7 GB). Under 2.91 it is PARTIAL_SUCCESS: 39 pages fail with `std::bad_alloc` (the Phase 2B failure), with 5.5 GB still free. The corpus takes 409 s vs 424 s (−4 %) but uses about 2.5× the CPU. One bounded span run was slower (155 s vs 102 s). |
| Does it reduce memory use? | **Yes for the GPU; slightly for RAM.** Under 2.91, CUDA memory grows by about 170 MB per conversion (0.85 → 4.98 GB over 26 documents); 2.143 stays flat at about 1.0–1.9 GB. Peak RSS is 2.6 GB vs 2.9–3.0 GB. |
| Does it introduce regressions? | **Yes, four.** (1) With Heron, two tables are lost on leaf-600 2026-06-01 (−14 Docling charges; hybrid unaffected). (2) Labels change from `Schedule HP &Schedule LGS-RTP` to `Schedule HP & Schedule LGS-RTP`, with the same values (3 documents). (3) TableFormer V2 is unusable for these tables. (4) Full-page OCR on digital PDFs loses values. Details in §8. |
| Is the stack compatible with the application and GPU? | **Yes.** `pip check` is clean. Full suite: **1681 passed, 92 skipped, 4 xfailed, 0 failed**, identical to `.venv312` on the same tree. All 101 Docling, structured and bounded tests pass. torch 2.14.1+cu130 runs on the RTX 4060 with driver 581.32. Legacy and native extraction are byte-identical under PyMuPDF 2.0 and pdfplumber 0.11.10. |
| Is upgrading worthwhile? | **Yes, but only together with the Egret-medium layout and a staged adoption (§10).** Upgrading the packages alone, with Heron, is a net accuracy regression for Docling mode. Its reliability wins: whole-document conversion, no VRAM growth, and working OCR. |

## 1. Dependency inventory

Versions in the validated environments. The current release and date come from PyPI and GitHub, retrieved 2026-10-27; the raw metadata is in `reports/phase2d/env/`.

| Package | `.venv312` installed (released) | `.venv312-ocr` | Current stable (released) | Python |
|---|---|---|---|---|
| docling | 2.91.0 (2026-04-23) | — | 2.143.1 (2026-10-27) | ≥3.10,<4 |
| docling-core | 2.74.1 (2026-04-22) | — | 2.105.0 (2026-10-27) | ≥3.10,<4 |
| docling-parse | 5.10.1 (2026-04-24) | — | 7.23.1 (2026-10-26) | ≥3.10 |
| docling-ibm-models | 3.13.2 (2026-04-23) | — | 4.0.4 (2026-10-21) | ≥3.10,<4 |
| PyMuPDF | 1.27.2.2 (2026-03-19) | 1.27.2.2 | 2.0 (2026-10-19) | **≥3.11** (new) |
| pdfplumber / pdfminer.six | 0.11.9 / 20251230 | same | 0.11.10 / 20260107 | ≥3.8 / ≥3.10 |
| pypdfium2 | 5.6.0 | 5.6.0 | 5.14.0 | ≥3.6 |
| PyTorch / torchvision | 2.6.0+cu124 (2025-01-29) / 0.21.0 | — | 2.14.1 (2026-09-30), cu126/cu128/cu130 wheels / 0.29.1 | ≥3.10 |
| Transformers | 5.6.2 (2026-04-23) | — | 5.21.0 (2026-10-26) | **≥3.11** |
| huggingface_hub | 1.12.0 | 1.9.2 | 2.3.0, but `docling-ibm-models` 4 requires <2 → **1.33.0** | ≥3.10 |
| RapidOCR | 3.8.1 (no onnxruntime installed) | — | 3.10.0 | ≥3.8 |
| PaddleOCR / PaddlePaddle / PaddleX | — | 3.4.0 / 3.3.1 / 3.4.3 | 3.7.0 / 3.3.1 / 3.7.2 | — |
| Tesseract (system) | 5.5.0.20241111 (UB-Mannheim build) | | 5.5.3 upstream (2026-07-24); no newer UB-Mannheim Windows build | — |
| Ollama (system) | 0.40.11 (auto-updated today) | | 0.40.12 (2026-10-27) | — |

CUDA: RTX 4060 Laptop GPU, 8 GB, driver 581.32 (supports CUDA 13.0). `.venv312` uses CUDA 12.4 with cuDNN 9.1; the next env uses CUDA 13.0 with cuDNN 9.24.

**Model artifacts.** These are downloaded models, distinct from the package versions above. Docling's specs fetch the repos' `main`. Both environments resolved the same commits:

| Model | Revision in `~/.cache/huggingface` (`.venv312`) | Revision in `.cache-docling-next` | Used by |
|---|---|---|---|
| docling-layout-heron | `8f39ad3c…` | `8f39ad3c…` (identical) | app default layout |
| docling-models (TableFormer v1 accurate/fast) | `fc0f2d45…` | `fc0f2d45…` (identical) | app default table structure |
| docling-layout-egret-medium | — | `77ede7cc…` (new) | config variant |
| TableFormerV2 | — | `e41ba5da…` (new) | config variant |

So the Heron/TableFormer runs in the two environments use byte-identical weights. Every difference between them comes from the code, not the models.

## 2. Release-note findings (2.91 → 2.143; filtered list in `reports/phase2d/env/release_notes_filtered.txt`)

| Version | Change | Relevance here |
|---|---|---|
| docling-parse 6.0 / docling 2.96 | Threaded docling-parse PDF backend | 2.123 makes it the default. Measured: CPU use 98 % → 260 %; it did **not** cause the leaf-600 loss (§5.3). |
| docling-parse 6.1, 7.12 | "Release unused python memory"; "Avoid 1M cells reserve to reduce memory footprint" | Consistent with the whole-document `bad_alloc` fix (§6). |
| docling-parse 7.0 (breaking) | New decode configuration, pages/sec | Internal; no application impact observed. |
| docling 2.103 | Layout: drop a PICTURE cluster that coincides with a TABLE | Layout post-processing changed. One candidate cause of the leaf-600 loss; not isolated further. |
| docling 2.109, 2.137 | RapidOCR defaults to PP-OCRv6 | The scanned-page gain (§7). |
| docling 2.116, 2.121, 2.136, 2.140 | Layout-driven OCR modes; better OCR input selection; "do not OCR programmatic text crossed by a vector shape"; "do not OCR an enclosed region twice" | OCR overhead on digital pages: auto OCR changed no digital-page values (§7). |
| docling 2.120.3 | Models are no longer `torch.compile`d by default | Lower first-call latency. |
| docling 2.122 | Faster layout and table-cell matching | Consistent with the small speed gain. |
| docling 2.139 | Threaded pipeline models run on long-lived threads | Consistent with flat VRAM across conversions (§6). |
| docling-ibm-models 4.0 (breaking) | Unused models removed; TableFormer post-processing fixes (4.0.1) | No API break for the app's `TableStructureOptions`. |
| PyMuPDF 2.0 | MuPDF 1.28.5; Python 3.11–3.15 only; deprecated `fitz` imports now emit `FutureWarning`; CVE-2026-82035 fix (CLI font extraction) | The app's `import fitz` keeps working. Legacy and native output are byte-identical (§5.1). |
| `DocumentConverter.convert(page_range=…)` | Present in both versions | Keeps original page numbers (§6). |

## 3. Isolated environment

- `.venv-docling-next`: Python 3.12.10 (same interpreter as `.venv312`).
- Resolution method:
  - torch 2.14.1+cu130 from the PyTorch cu130 index;
  - the target packages pinned to current releases (`reports/phase2d/env/next_targets.txt`);
  - every other `.venv312` pin kept (`next_base_from_venv312.txt`).
- Five pins were relaxed only because the new stack requires it: setuptools, sympy, requests, pillow and click (`next_relaxed_pins.txt`).
- `pip check`: no broken requirements.
- Full resolved graph: `reports/phase2d/env/venv-docling-next_freeze.txt`. Validated baselines: `venv312_freeze.txt`, `venv312-ocr_freeze.txt`.
- The app is installed editable without dependencies, so the same `src/` runs in both environments.
- `HF_HOME=.cache-docling-next/hf` keeps new model downloads out of the shared cache.
- Both directories are added to `.gitignore`.

Compatibility notes:
- rapidocr 3.10 pulls `opencv-python-headless` 5.0 next to `opencv-python` 4.13. `cv2` resolves to 4.13 and nothing failed, but two packages now provide `cv2`.
- Click 8.5 emits deprecation warnings through typer 0.21.

## 4. Benchmark design

- **Same bytes for every environment.** Each corpus document's span (the union of the transcribed pages and `hd_bounds`) is cut once with `.venv312`'s PyMuPDF into an excerpt PDF (`bench/phase2d/make_excerpts.py`; manifest `reports/phase2d/excerpt_manifest.json`). The envelope records `page_offset`, so pages keep their filing numbers.
- **Same application conversion code.** `bench/phase2d/convert_corpus.py` calls the app's `docling_backend.convert_pdf_with_docling(force=True)`. Configuration variants only patch the pipeline options the app builds.
- **Same extraction code and environment.** `bench/run_extraction.py --docling-cache <dir>` runs the 26-document benchmark from `.venv312` against each cache, so only the Docling output varies. `diff_runs.py` diffs charges field by field.
- **Reproduction check.**
  - A fresh 2.91 conversion of every excerpt gives Docling-mode charges identical to the committed cache.
  - Legacy, native, Docling and hybrid runs on the committed cache reproduce Phase 2C exactly.
  - A second 2.91 run and a second 2.143 run are structurally identical to the first. Conversion is deterministic.
- **Resources.** Per document, a sampler thread records:
  - process-tree RSS, CPU %, and the minimum system available memory;
  - `torch.cuda.max_memory_allocated/reserved` (per process);
  - device VRAM and GPU utilisation from `nvidia-smi` (200 ms).
- **Watchdog.** It aborts a run below a free-memory floor; no run tripped it.

**Measurement caveats.**
- First runs started with 0.6 GB available. Desktop apps and WSL/Docker held the memory; Ollama had no models loaded. You chose to proceed under pressure. Memory later rose to 6–10 GB, so the "r2" reruns are the timing reference.
- The first document of every run includes model loading.
- Device-level VRAM in later runs is contaminated by a concurrent pytest run. The torch per-process figures are authoritative.
- Timings are single runs.

## 5. Accuracy results (26 documents, 798 expected charges)

### 5.1 Text and native paths (PyMuPDF 2.0, pdfplumber 0.11.10)

| | legacy `.venv312` | legacy next | native `.venv312` | native next |
|---|---:|---:|---:|---:|
| Recall / precision | 0.7607 / 0.9806 | identical | 0.6792 / 0.9801 | identical |
| Field diff | — | **IDENTICAL** (incl. evidence pages) | — | **IDENTICAL** |

### 5.2 Docling mode (`reports/phase2d/bench/scores_docling_all.md`)

| Metric | committed cache | 2.91 app | 2.91 + Egret-M | **2.143 app** | 2.143 non-threaded | **2.143 + Egret-M** | 2.143 + TableFormer V2 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Recall | 0.7356 | 0.7356 | 0.7682 | 0.7168 | 0.7168 | **0.7682** | 0.3045 |
| Precision | 0.9800 | 0.9800 | 0.9808 | 0.9795 | 0.9795 | 0.9808 | 0.9643 |
| Extracted / matched | 599 / 587 | 599 / 587 | 625 / 613 | 584 / 572 | 584 / 572 | 625 / 613 | 252 / 243 |
| Unit accuracy | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| Rate-class accuracy (summaries) | 1.0 | 1.0 | 1.0 | 0.980 | 0.980 | 1.0 | 0.943 |
| Rider TOTAL reconciliation | 0.641 | 0.641 | 0.680 | 0.577 | 0.577 | **0.680** | 0.180 |
| Zero-document false positives | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Page / bbox / quote provenance | 0.987 | 0.992 | 0.992 | 0.991 | 0.991 | 0.994 | 0.980 |
| Table-cell provenance | 0.880 | 0.880 | 0.877 | 0.884 | 0.884 | 0.877 | 0.853 |

### 5.3 Hybrid mode (the candidate default; `scores_hybrid_all.md`)

| Metric | 2.91 app | 2.91 + Egret-M | 2.143 app | **2.143 + Egret-M** | 2.143 + TableFormer V2 |
|---|---:|---:|---:|---:|---:|
| Recall / precision | 0.7732 / 0.9809 | same | same | same | 0.7644 / 0.9591 |
| Charges vs 2.91 app | — | identical | identical | identical | 7 lost, 7 false positives |
| TOTAL reconciliation | 0.718 | 0.718 | 0.718 | 0.718 | 0.718 |
| Page provenance (matched) | 0.953 | 0.984 | **0.919** | **0.985** | 0.362 |
| Table-cell provenance | 0.835 | 0.868 | 0.805 | 0.868 | 0.321 |

Hybrid keeps the text value whenever the parsers disagree, so its charges never move. Only the share of charges with table evidence changes.

### 5.4 Structure: table detection and table-cell recovery (`reports/phase2d/conversion_compare_all.md`)

"Recovery" is the share of 690 hand-transcribed non-TOTAL values whose printed digits appear in some Docling table cell.

| | 2.91 app | 2.91 + Egret-M | 2.143 app | 2.143 + Egret-M | 2.143 + TableFormer V2 |
|---|---:|---:|---:|---:|---:|
| Tables / cells | 138 / 8956 | 120 / 6097 | 134 / 7478 | 132 / 7648 | 134 / 12459 |
| Table-cell recovery | 0.9174 | 0.9217 | 0.9145 | **0.9217** | 0.9116 |
| Any-text recovery | 0.9594 | 0.9594 | 0.9594 | 0.9594 | 0.9565 |
| Provenance inside the span | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |

## 6. Known problem documents

| Document | 2.91 (Heron) | 2.143 (Heron) | 2.143 + Egret-M | Verdict |
|---|---|---|---|---|
| **DEC blk304** (Docling missed tables) | 4 tables, 59/64 values in cells, Docling 36/72 | same | **6 tables, 64/64, Docling 54/72** (= legacy) | Fixed by Egret, in both versions. Not by the upgrade itself. |
| **DEC blk307** (multiple rate classes; one printed TOTAL wrong) | 6 tables, 82/82, 68/90 | same | same | Unchanged. The data issue lives in the source PDF. |
| **DEP leaf-600 2026-06-01** | 14 tables, 108/108, 125/135 | **12 tables, 106/108, 111/135** | 14 tables, 108/108, 125/135 | **Regression in 2.143 with Heron.** Page 3's Demand-MGS TOTAL continuation and the Seasonal/Intermittent table become plain text. The non-threaded backend reproduces the loss, so the cause is layout and post-processing code, not the parser. Egret detects both tables. |
| DEP leaf-600 2026-04-01 | 122/135, c0 | 121/135, **11 rate-class errors** | 122/135, c0 | Regression with Heron; HP table gains a row (14 → 15). |
| **Scanned DEC RS (hd 7380)** | OCR off: 13/31 in cells | OCR off 13/31; **RapidOCR auto 29/31**; Tesseract 26/31 | — | The OCR gain is real but does not reach charges: 0/29 in every mode. The text comes from the DB `docling_artifact_sliced` source, and the DEC RS profile is not table-structured (§7). |
| Multi-column JAA (hd 0179, 7365, 5324; expected zero) | 0 false positives | 0 | 0 | Unchanged. 2.143 emits far fewer text items for the same pages (hd 5324 span: 961 → 126), with merged paragraphs. Harmless here. |
| **Redlined DEP blk13** | 99/135 (Docling), 100 (hybrid) | same values; HP labels re-spaced | same | No change in fused-digit handling. |
| **110-page JAA, whole document (hd 5324)** | **PARTIAL_SUCCESS**: `std::bad_alloc` on pages 44–58 and 87–110 (39 pages); 86 tables; 502 s; 3.6 GB RSS; 5.5 GB still free | **SUCCESS**: all 110 pages; 128 tables; 792 s; 3.7 GB RSS | — | Fixed by the upgrade. The Phase 2B failure is an allocation failure in docling-parse v5, not system exhaustion. |

## 7. OCR configuration

Four documents in both environments (`reports/phase2d/ocr_compare.md`, `reports/phase2d/bench_ocr/scores.md`): three digital, and hd 7380, which is scanned.

| Mode | 2.91 | 2.143 | Digital docs (values in cells) | Scanned hd 7380 (cells / any text) | Docling charges, leaf-600 06-01 |
|---|---|---|---|---|---|
| off (app default) | ok | ok | leaf-600 108 (2.91) / 106 (2.143); blk304 59; hd 29: 0/7 | 13/31 / 0.65 | 125 / 111 |
| auto (`OcrAutoOptions`) | ok, **no effect** | ok (RapidOCR PP-OCRv6) | unchanged | 2.91: 13/31 · **2.143: 29/31 / 1.0** | – / 111 |
| Tesseract CLI (app's scanned route) | **segfault, exit 139** | ok | unchanged | 26/31 | – / 111 |
| full-page Tesseract | **segfault** | ok | leaf-600 **103** (worse) | 26/31 | – / **56** |

Findings:
- **Digital PDFs.** 2.143's OCR selection adds no values on digital pages, and the extra time is small. hd 29 went 3.9 s → 3.7 s in auto mode; leaf-600 timings are dominated by model loading.
- **Full-page OCR is harmful.** It replaces good programmatic text: Docling-mode leaf-600 drops 111 → 56. Hybrid absorbs this (125). Keep OCR off for documents with a text layer.
- **The app's own scanned route crashes on the validated stack.** `has_scanned_pages=True` (Tesseract CLI) segfaults the 2.91 process on Windows. 2.143 fixes it.
- **Scanned pages.** RapidOCR auto is the best of the existing engines, with 29/31 values present. Turning that into charges needs a profile that consumes Docling tables for DEC RS, which is application work outside this phase.

## 8. Performance and memory

Corpus conversion, 26 excerpts, CUDA, app settings. The r2 runs had 3–7 GB free (`reports/phase2d/resource_summary.md`).

| Run | Total s | Excluding the first doc (model load) | hd 7365 | hd 5324 span | Peak RSS | torch peak alloc / reserved | Mean CPU % | Mean GPU util % |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2.91 app (r2) | 463 | 424 | 100 | 140 | 3003 MB | **4977 / 5210 MB** | 98 | 25 |
| 2.143 app (r2) | 448 | 409 | 85 | 136 | 2625 MB | **1871 / 2178 MB** | 260 | 26 |
| 2.143 non-threaded | 694 | 668 | 268 | 211 | 2727 MB | 1870 / 2178 MB | 193 | 48 |
| 2.91 + Egret-M | 727 | 645 | 165 | 142 | 4036 MB | 3000 / 3356 MB | 107 | 31 |
| 2.143 + Egret-M | 441 | 401 | 96 | 116 | 2664 MB | 1647 / 1964 MB | 261 | 26 |
| 2.143 + TableFormer V2 | 381 | 347 | 64 | 110 | 2600 MB | 1969 / 5132 MB | 264 | 22 |

**GPU memory growth.** The app builds a new converter on every call.
- Under 2.91 the CUDA allocation climbs from document to document: 852, 1228, … 4977 MB, about 170 MB per conversion.
- Under 2.143 it returns to about 1.0 GB after each document.
- On an 8 GB GPU, a long single-process batch under 2.91 would run out of VRAM after roughly 40 documents. This is the most significant reliability difference.

**Large documents** (hd 5324, 110 pages, span 17–31; `reports/phase2d/runs/mem/`):

| Scenario | 2.91 | 2.143 |
|---|---|---|
| Native span (pdfplumber/PyMuPDF) | 1.7 s, 99 MB | 2.0 s, 177 MB |
| App bounded conversion (`bounded.py`, 8-page chunks) | **ok**: 15 pages (17–31), 27 tables, 102 s, 2.1 GB | **ok**: same pages and tables, 155 s, 2.3 GB |
| Docling `page_range=(17,31)` on the original PDF | ok: pages 17–31, 27 tables, 139 s, 2.6 GB | ok: pages 17–31, 27 tables, 174 s, 2.3 GB |
| Whole document | PARTIAL: 39 pages `bad_alloc`, 502 s, 3.6 GB | **ok**: 110 pages, 792 s, 3.7 GB |

- Phase 2C's unmeasured bounded Docling scenario is now measured, and it succeeds in **both** versions once memory is available.
- Bounded conversion and `page_range` both keep original page numbering and the same 27 span tables. `page_range` could replace PyMuPDF excerpting, but in these runs it was not faster.
- For the span scenarios 2.143 was slower: 155 vs 102 s and 174 vs 139 s, single runs, with background load varying. The corpus-level hd 5324 span time was equal (136 vs 140 s). Treat large-document speed as unchanged, not improved.

## 9. Regressions and new failures

1. **Heron table loss on DEP leaf-600 2026-06-01.** Two page-3 tables are lost; Docling mode drops 14 charges; TOTAL reconciliation falls 0.641 → 0.577; hybrid evidence coverage falls 0.953 → 0.919. Hybrid charges are unchanged. Avoided with Egret-medium.
2. **Label text normalization.** `Schedule HP &Schedule LGS-RTP` becomes `Schedule HP & Schedule LGS-RTP` in leaf-600 (both revisions) and blk13: 35 charges with the same values. In 2.143 Docling now emits a space the PDF's glyph positions imply. Candidate runs from the new stack will show these as label changes in `provenance compare`.
3. **TableFormer V2** (`TableStructureV2Options`) is unusable for these profiles. It produces more, smaller cells: Docling recall falls to 0.30, and hybrid gains 7 false positives. Do not adopt it.
4. **Full-page OCR on digital PDFs** loses values (§7). This is not a default, but it must not be enabled globally.
5. **Packaging.** A second `cv2` provider (opencv-python-headless 5.0) is installed, and Click 8.5 emits deprecation warnings. PyMuPDF 2.0 and Transformers 5.21 require Python ≥ 3.11, which is fine for 3.12.

No new test failures; the suite is identical in both environments.

## 10. Recommendation and staged adoption

**Recommended set** (`.venv-docling-next`, frozen in `reports/phase2d/env/venv-docling-next_freeze.txt`):

| Item | Recommendation |
|---|---|
| docling / -core / -parse / -ibm-models | 2.143.1 / 2.105.0 / 7.23.1 / 4.0.4. Released between 2026-10-21 and 2026-10-27; re-check for a .x patch before adoption. |
| torch / torchvision | 2.14.1+cu130 / 0.29.1+cu130 (driver 581.32 OK) |
| transformers / huggingface_hub | 5.21.0 / 1.33.0 (capped <2 by docling-ibm-models) |
| PyMuPDF / pdfplumber | 2.0 / 0.11.10: no output change. Keep `import fitz` working, or migrate to `import pymupdf` to silence the `FutureWarning`. |
| Layout model | **docling-layout-egret-medium**, pinned to revision `77ede7cc…`, instead of Heron |
| Table structure | TableFormer v1 ACCURATE, `do_cell_matching=True` (unchanged) |
| OCR | Off for documents with a text layer. RapidOCR auto only on the scanned-document route. Never full-page OCR on digital PDFs. |
| Large filings | Keep bounded span conversion (works in both versions). Whole-document conversion is now viable but slow (~7 s/page). |
| PaddleOCR / Ollama / Tesseract | Not on the Docling path. No upgrade needed for this phase (`.venv312-ocr` untouched). |

**Staged plan.** Each stage is a separate implementation decision.

1. **Stage 0 (done).** Isolated environment, lockfiles, benchmarks, this report.
2. **Stage 1: make the layout model a configuration option** in `docling_backend._build_standard_converter`.
   - Keep the Heron default. Add the layout and model revision to `DOCLING_BACKEND_VERSION`, or to the artifact-cache key, so new artifacts never mix with old ones.
   - Optionally reuse one converter per process. That removes the 2.91 VRAM growth on the current stack too.
   - `docling_backend` is GitNexus-CRITICAL. Run impact analysis first, and re-index, because the index is stale.
3. **Stage 2: rebuild the benchmark Docling cache** with 2.143 + Egret-medium.
   - Re-run the 26-document benchmark, the full suite and the live 12-bill reconciliation in the new environment.
   - Decide how to handle the HP label re-spacing: accept it as a label change, or normalize whitespace in the profile.
4. **Stage 3: promote the environment.**
   - Create the new validated venv from the frozen lockfile (e.g. `.venv312-next`). Keep `.venv312` as rollback for at least one phase.
   - Switch DEV scripts and overnight jobs (`pwsh`) to it.
5. **Stage 4: reconvert production Docling artifacts** under the new backend version, family by family.
   - Run `provenance extract-candidates --mode hybrid`, review with `candidates --needs-review` / `compare`, and promote only reviewed runs.
   - Production still needs `provenance migrate` first (Phase 2C §11).
6. **Stage 5 (application work): scanned-document route.**
   - RapidOCR auto for image-only pages, and a table-aware DEC RS profile so the 29/31 recovered values become charges.

## 11. Regression validation and production integrity

| Check | Result |
|---|---|
| 26-document benchmark, every mode, both environments | §5. Phase 2C numbers reproduced exactly on the committed cache. |
| Docling, structured and bounded tests in the next env (9 files) | 101 passed |
| Full suite, `.venv312` (current tree) | 1681 passed, 92 skipped, 4 xfailed, 0 failed (12:45) |
| Full suite, `.venv-docling-next` | 1681 passed, 92 skipped, 4 xfailed, 0 failed (11:54) |
| Frozen 12-bill reconciliation | **Not rerun.** No application code changed; it is required at Stage 2. |
| Candidate runs or promotions | None. The benchmark read a scratch copy of `duke_rates.db.bak_pre_phase0_20261026`, read-only. |
| Production SHA-256 before / after | `fe563a783cc78d6bb1a561685f1d83c81d7ea4c87729472ede96e192e888cd08` / identical; mtime still 2026-10-26 12:36 (`reports/phase2d/prod_sha_*.txt`) |
| GitNexus | `make_representation_provider` (bench) is not in the index. A text search found one caller, in the same file, so the new option is additive with an unchanged default. |

## 12. Files

| Path | Content |
|---|---|
| `bench/phase2d/make_excerpts.py` | Span excerpts cut once, with the manifest |
| `bench/phase2d/convert_corpus.py` | Conversion through the app backend; variants; RSS, VRAM, CPU and GPU sampling; low-memory watchdog |
| `bench/phase2d/compare_conversions.py` | Table detection, cell recovery and provenance-span metrics |
| `bench/phase2d/diff_runs.py` | Field-by-field diff of extraction runs |
| `bench/run_extraction.py` | New `--docling-cache` option; default unchanged |
| `reports/phase2d/env/` | Freezes for all three environments, target and relaxed pins, PyPI metadata, release notes |
| `reports/phase2d/bench/`, `bench_ocr/` | Extraction run JSON, scores, field diffs |
| `reports/phase2d/runs/` | Per-run metrics, logs, driver scripts, bounded-memory JSON |
| `reports/phase2d/conversion_compare*.md/json`, `ocr_compare.*`, `resource_summary.md` | Structural, OCR and resource tables |

Reproduce:

```bash
python bench/phase2d/make_excerpts.py --out <dir>
HF_HOME=.cache-docling-next/hf .venv-docling-next/Scripts/python.exe bench/phase2d/convert_corpus.py --excerpts <dir> --out <run> --variant egret_m
.venv312/Scripts/python.exe bench/run_extraction.py --src src --db <scratch copy> --mode hybrid --docling-cache <run> --label x --out x.json
```
