# Document Intelligence Architecture

**Status:** Incremental implementation in active use
**Updated:** 2026-04-08

## Purpose

This document defines the incremental document-intelligence layer that extends the
existing Duke/NCUC parsing pipeline without replacing the current working system.

The target is not "rewrite parsing with AI." The target is:

1. normalize document evidence into a reusable representation
2. classify/fingerprint documents before extraction
3. map extraction outputs into structured schemas
4. validate structured outputs against source text
5. persist ML-ready training records for future learning loops

## Existing Architecture Summary

The current system already has strong foundations:

- page-aware historical intake in `src/duke_rates/historical/ncuc/pipeline/`
- reusable page and span artifacts in:
  - `ncuc_page_artifacts`
  - `ncuc_span_artifacts`
- parser-profile routing in
  [parser_profiles.py](/c:/Python/Duke/Standalone/src/duke_rates/historical/ncuc/pipeline/parser_profiles.py)
- diagnostic persistence in:
  - `document_fingerprints`
  - `parse_attempt_logs`
  - `parse_review_outcomes`
  - `historical_processing_runs`
- existing Pydantic models in:
  - [models/pipeline.py](/c:/Python/Duke/Standalone/src/duke_rates/models/pipeline.py)
  - [models/parse_result.py](/c:/Python/Duke/Standalone/src/duke_rates/models/parse_result.py)

## Current Strengths

- deterministic historical extraction already works for many document classes
- page/span artifact caching is already in place
- parser selection, fallback, and reprocess queueing already exist
- provenance and review metadata are already persisted

## Current Weaknesses

- document understanding is spread across heuristics, parser selection, and ad hoc metadata
- no unified normalized `DocumentRepresentation`
- existing `document_fingerprints` are useful but narrower than a full document-intelligence snapshot
- validation is mostly parser/outcome-oriented, not schema-oriented
- there is no standardized ML-ready training record output

## Integration Strategy

The new layer is intentionally additive.

It does **not** replace:
- page mining
- span segmentation
- parser profiles
- charge insertion

It wraps and enriches them.

## New Module Structure

`src/duke_rates/document_intelligence/`

- `models.py`
- `representation.py`
- `normalization.py`
- `fingerprinting.py`
- `extraction.py`
- `validation.py`
- `dataset.py`
- `service.py`

## Current Normalization Layer

The document-intelligence flow now starts with an additive normalization router
instead of assuming bounded plain text is always sufficient.

Implemented backends:

- `NativePdfNormalizer`
  - uses existing text-layer / page-artifact evidence first
  - preserves the current fast path for clean PDFs
- `PaddleStructureNormalizer`
  - optional `PaddleOCR` / `PP-Structure` backend
  - meant for scanned, layout-heavy, or table-heavy pages
  - preserves blocks and tables in the normalized representation
- `GlmOcrNormalizer`
  - optional Ollama-hosted `glm-ocr` page OCR fallback
  - intended for difficult pages, poor reading order, or low-text failures

Routing is handled by `DocumentNormalizationRouter`, which favors:

1. existing page-artifact/native text extraction
2. Paddle structure OCR when native text is weak
3. GLM-OCR as a selective fallback or page-level escalation path

The current router now also supports page-level escalation for symbol-noise cases, not just low-text failures.
Examples include pages where native extraction produces artifacts like `cVkWh`, `S/kWh`, or merged numeric values.
This is intended to reduce custom parser cleanup for OCR damage when a better page-level OCR lane can recover the source text.

This is intentionally additive. It does not replace parser profiles or charge
extraction. It improves the normalized evidence that existing downstream logic
receives.

## Normalized Representation

`DocumentRepresentation` now acts as the common normalized abstraction and
captures:

- source PDF path and provenance
- normalizer backend used
- raw text and optional markdown text
- per-page normalized text
- layout blocks with optional bounding boxes
- extracted tables with optional markdown / HTML
- normalization warnings
- normalization metrics such as:
  - page count
  - text length
  - low-text page count
  - table page count
  - GPU usage
  - elapsed runtime

## Current Integration Point

The first live integration point is the historical bulk extractor:

- [bulk_extractor.py](/c:/Python/Duke/Standalone/src/duke_rates/historical/ncuc/pipeline/bulk_extractor.py)

This is the safest place to start because it already has:
- bounded text
- parser profile selection
- charge counts
- outcome status
- processing-run metadata

The new layer enriches that existing flow and persists training records without
changing charge extraction semantics.

## Migration Plan

### Phase 1

- add normalized document-intelligence models
- add fingerprinting / schema / validation / dataset modules
- integrate metadata capture into historical bulk extraction

### Phase 2

- route orchestrator input through pluggable normalization backends
- keep native extraction as the cheapest/default viable path
- expose backend/quality metadata in training records
- add reusable reporting around document-intelligence quality

### Phase 3

- add richer OCR/layout/table comparison utilities
- add explicit backend benchmarking/report surfaces
- add pluggable LLM-assisted classifiers/extractors
- generate labeled training corpora from review outcomes and corrected records

## Laptop GPU Notes

The OCR backends are being tuned for local Windows laptop execution, not
datacenter hardware.

Current design choices:

- conservative page batching
- explicit GPU preference flags instead of mandatory CUDA assumptions
- safe CPU fallback when Paddle GPU initialization or Ollama availability fails
- selective page-level GLM escalation instead of whole-document LLM OCR by default

Recommended operating model:

- keep native text extraction as the default for clean PDFs
- use Paddle as the primary OCR/layout backend
- use GLM-OCR sparingly for suspicious pages or backend disagreement

## Non-Goals For This Phase

- replacing the working parser-profile system
- replacing current tariff charge insertion logic
- introducing provider-specific LLM dependencies
- rewriting the DB schema around a new abstraction
