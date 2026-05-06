# Docling Integration Plan

## Purpose

This document describes how `Docling` should be integrated into the NCUC
historical parsing pipeline.

## Current Implementation Status (as of 2026-03-27)

### Steps 1–4: Completed (Docling backend and artifact caching)

- `src/duke_rates/historical/ncuc/pipeline/docling_backend.py` — backend wrapper with
  availability guard, file-hash cache, sidecar artifact writing (JSON / text / tables)
- `src/duke_rates/db/artifact_cache.py` — `save_docling_artifact` / `load_docling_artifact`
- `src/duke_rates/db/schema.py` — `docling_artifacts` table
- `src/duke_rates/historical/ncuc/pipeline/stage_versions.py` — `DOCLING_BACKEND_VERSION`
- `pyproject.toml` — `[project.optional-dependencies] docling` extra
- CLI command `run-docling-nc <pdf_path>` for operator-requested pilot runs
- `tests/test_docling_backend.py` — focused tests for cache, sidecar, and DB round-trip

### Steps 5–8: Completed (Bridge into page-aware pipeline)

- `src/duke_rates/historical/ncuc/pipeline/docling_page_miner.py` — reconstructs `PageEvidence`
  from stored Docling artifacts using existing text-based feature extraction
- `src/duke_rates/historical/ncuc/pipeline/stage_versions.py` — `DOCLING_PAGE_MINER_VERSION`
  artifact version (separate from native-text `PAGE_ARTIFACT_VERSION` to prevent collisions)
- CLI command `mine-docling-nc` — selective operator path to bridge artifacts into
  page/span/family/parse artifacts using existing importer logic
- `tests/test_docling_page_miner.py` — focused tests for Docling-to-PageEvidence reconstruction

The bridge reuses existing pipeline stages entirely:
- `save_page_artifacts()` / `save_span_artifacts()` — store reconstructed evidence
- `segment_document()` — segment pages into spans unchanged
- `find_best_family_for_span()` — family matching unchanged
- `HistoricalRateParserRegistry.select()` — parser profile selection unchanged
- existing `repository.upsert_historical_document()` — document linkage unchanged

The intended role is:
- optional
- selective
- artifact-oriented
- compatible with the existing CPU-first parser pipeline

It is **not** intended to replace:
- discovery/search
- family matching
- parser profiles
- review/reprocess logic
- deterministic tariff extraction as the source of truth

## Why Docling Is Relevant

Docling can provide richer document artifacts than plain OCR text alone:
- structured document JSON
- layout-aware reading order
- table structure
- OCR output
- confidence grades
- chunking/serialization suitable for later LLM analysis

That makes it a good fit for:
- hard scanned PDFs
- table-heavy rider summaries
- mixed-layout compliance books
- repeated weak/empty parses
- future explanation / relationship-discovery workflows

## Intended Placement In The Existing Pipeline

Docling should sit behind the existing triage and weak-parse logic.

Preferred order:
1. native-text path (`PyMuPDF` / current mining)
2. existing CPU OCR path (`pytesseract`)
3. Docling path for structurally difficult documents
4. optional GPU-backed Docling runs for pilot/performance-sensitive hard cases

Docling should feed the existing system, not bypass it:
- importer
- page miner
- family matcher
- parser profiles
- artifact cache
- reprocess queue
- review/reporting

## Planned Integration Points

Likely code locations:
- `src/duke_rates/historical/ncuc/pipeline/docling_backend.py`
- `src/duke_rates/historical/ncuc/pipeline/docling_router.py`
- `src/duke_rates/db/artifact_cache.py`
- `src/duke_rates/db/schema.py`

Likely data to persist:
- Docling JSON output
- plain-text export
- table exports
- conversion confidence grades
- backend / accelerator metadata
- source file hash
- page-level conversion summaries

## Routing Policy

Do **not** run Docling on every PDF.

Initial recommended routing:
- `OCR_REQUIRED` + high structure complexity
- `gpu_ocr_candidate`
- repeated weak/empty parses
- rider-summary / matrix pages where table structure matters
- operator-requested analysis for document understanding

Do **not** route clean native-text PDFs into Docling by default.

## Cache / Invalidation Plan

Docling outputs should be cached by:
- `source_pdf`
- file hash
- Docling backend version
- accelerator mode
- major pipeline option set

Docling artifacts should be invalidated only when:
- source hash changes
- Docling version/backend changes
- critical Docling options change

This keeps Docling from becoming an expensive reprocessing tax.

## Suggested Pilot

Start with a narrow validation sample:
- 1 scanned OCR-heavy tariff sheet
- 1 rider-summary table page
- 1 mixed compliance-book span
- 1 weak/empty historical document that current OCR/text path struggles with
- 1 document that is structurally complex but not scanned

For each sample, compare:
- current pipeline output
- current OCR path output
- Docling CPU output
- Docling CUDA output (if available)

Measure:
- runtime
- extraction completeness
- table recovery quality
- family-match quality
- downstream parser-profile usefulness

## GPU Evaluation Plan

Docling GPU should be treated as an evaluation path first.

Test:
- CPU Docling
- CUDA Docling

Questions to answer:
- Is CUDA materially faster on the PDFs that matter?
- Does CUDA improve OCR/layout quality, or only speed?
- Does it justify the operational complexity?

Only after that comparison should Docling CUDA be promoted into a real queue.

## Relationship / LLM Analysis Use

Docling is especially promising for the planned document-intelligence layer.

Useful outputs for that layer:
- structured chunks
- section-aware serialization
- page-level confidence
- table-aware text

That can improve:
- document-purpose explanations
- relationship mapping
- relevance classification
- identification of future extraction targets

LLM guardrail remains the same:
- use LLMs for explanation, suggestion, and enrichment
- do not silently replace deterministic parsed tariff facts

## Recommended Implementation Order

1. ~~Add Docling planning/docs only~~ ✓
2. ~~Add optional dependency path~~ ✓
3. ~~Add artifact schema/cache support~~ ✓
4. ~~Build a narrow backend wrapper~~ ✓
5. Pilot on a small NCUC sample
6. Compare CPU vs CUDA
7. Decide selective routing policy
8. Integrate into review/reprocess workflows

## Success Criteria

- Docling improves hard-document handling without slowing the default path
- artifacts are cached and reusable
- CPU vs CUDA tradeoffs are measured, not guessed
- Docling enriches the existing pipeline instead of fragmenting it
