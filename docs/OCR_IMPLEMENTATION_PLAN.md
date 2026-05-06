# OCR And Complex Document Processing Plan

## Current State

The codebase now has the routing primitives needed for a staged OCR plan:

- `triage.py` detects likely scanned PDFs and marks them `OCR_REQUIRED`
- `DocumentTriage` now carries:
  - `ocr_confidence_score`
  - `structure_complexity_score`
  - `gpu_ocr_candidate`
  - `triage_flags`
- the historical pipeline has a parser-profile seam in
  `historical/ncuc/pipeline/parser_profiles.py`
- the normal historical import path remains CPU-first and native-text-first

This means OCR no longer needs to be treated as an all-or-nothing pipeline
rewrite. It can be added as a selective escalation path.

This plan also reserves space for `Docling` as an optional structured
document-conversion backend for hard PDFs. The intended role is to enrich the
existing pipeline with better layout/table artifacts, not to replace the
current CPU-first path.

## Design Principle

Use CPU-first triage and native-text extraction for the default path.

Only escalate to OCR when:
- the document is likely scanned
- native text extraction is too sparse to support parsing
- structure is complex enough that standard text/table extraction is unreliable

Only escalate to GPU-backed OCR/layout when the OCR case is strong enough to
justify the additional infrastructure cost.

## Target Routing Model

### Route 1: Native text CPU path

Use existing tools first:
- PyMuPDF for fast text-density and page sampling
- `pdfplumber` for page text and basic table extraction
- parser profiles for family/company/format-specific extraction

Applies when:
- text density is normal
- the document is not marked `OCR_REQUIRED`
- structure complexity is manageable

### Route 2: CPU OCR path

Use OCR for likely scanned documents that do not yet justify GPU escalation.

Recommended default:
- `OCRmyPDF` with sidecar text output, or
- `pytesseract` for page-level OCR when full-document OCR is not needed

Applies when:
- `route_recommendation == OCR_REQUIRED`
- `gpu_ocr_candidate == False`

### Route 2b: Structured Docling path

Use Docling when the document needs more than plain OCR text.

Recommended uses:
- OCR-heavy scans where layout matters
- scanned or native PDFs with complex tables
- mixed-layout compliance books
- documents repeatedly landing in weak/empty parse review

Recommended output:
- Docling JSON / structured document output
- plain text export
- table exports
- conversion confidence grades

Applies when:
- OCR/text extraction exists but is structurally weak
- table layout is important for extraction
- an operator or queue policy explicitly escalates to Docling

### Route 3: GPU OCR / layout path

Use GPU only for hard cases.

Recommended candidate:
- `PaddleOCR` for low-quality scans, image-heavy pages, and scanned tables

Applies when:
- `gpu_ocr_candidate == True`
- CPU OCR confidence is low, or
- table/layout structure is complex enough that a stronger model is warranted

## Planned Components

### 1. OCR processor module

Create:
- `src/duke_rates/historical/ncuc/pipeline/ocr_processor.py`

Responsibilities:
- choose OCR backend (`ocrmypdf`, `tesseract`, `paddleocr`)
- run OCR on PDF or page images
- emit normalized text artifacts
- capture OCR confidence / method / failure metadata

### 2. OCR work queue

Create a separate queue for OCR work instead of mixing it into normal import.

Suggested schema additions:

```sql
ALTER TABLE historical_documents ADD COLUMN ocr_source BOOLEAN DEFAULT FALSE;
ALTER TABLE historical_documents ADD COLUMN ocr_confidence FLOAT;
ALTER TABLE historical_documents ADD COLUMN ocr_method TEXT;

CREATE TABLE ocr_processing_queue (
    id INTEGER PRIMARY KEY,
    discovery_record_id INTEGER,
    file_path TEXT,
    status TEXT,
    method TEXT,
    priority INTEGER DEFAULT 0,
    ocr_confidence FLOAT,
    structure_complexity FLOAT,
    gpu_candidate BOOLEAN DEFAULT FALSE,
    error_message TEXT,
    created_at TIMESTAMP,
    processed_at TIMESTAMP
);
```

### 3. OCR artifact cache

Persist:
- OCR text sidecars
- page image hashes
- OCR method used
- parser profile selected after OCR
- optional Docling JSON / text / table artifacts
- Docling backend and accelerator metadata
- Docling conversion confidence grades

This avoids re-running expensive OCR when only downstream parsing changes.

### 4. Parser-profile expansion

Use OCR output to feed parser profiles, not a separate one-off extraction stack.

Priority profiles:
1. `progress_residential_tou`
2. `progress_rider_adjustment_matrix`
3. `carolinas_residential_flat`
4. `legacy_duke_power_sheet`
5. `generic_residential_fallback`

## Implementation Phases

### Phase 1. CPU-first OCR baseline

- [x] add OCR triage signals to `DocumentTriage`
- [x] mark high-confidence scanned and structurally complex docs
- [x] implement CPU OCR backend with `pytesseract`
- [x] generate OCR sidecar text for `OCR_REQUIRED` documents
- [ ] feed OCR text back into page mining / segmentation / parser profiles

### Phase 2. OCR queue and review workflow

- [x] add `ocr_processing_queue` table
- [x] add `ocr_artifacts` cache table
- [x] add CLI to enqueue OCR candidates
- [x] add CLI to process queue in batches
- [ ] add confidence thresholds for `success` vs `manual_review`
- [x] preserve OCR artifacts for reruns and auditability
- [x] invalidate OCR sidecars automatically when backend version changes

### Phase 3. GPU selective escalation

- [x] mark `gpu_ocr_candidate` during CPU triage
- [ ] implement optional GPU OCR backend with `PaddleOCR`
- [ ] route only flagged documents/pages to the GPU backend
- [ ] compare CPU OCR vs GPU OCR on a validation sample
- [ ] keep GPU path optional and isolated from the default install

### Phase 3b. Docling pilot

- [x] add optional Docling dependency path
- [x] add artifact-cache support for Docling JSON / text / table outputs
- [ ] add selective routing policy for Docling candidates
  - backend wrapper and CLI command implemented 2026-03-27; routing/queue integration planned
- [ ] compare current extraction vs Docling on a narrow NCUC sample
- [ ] compare Docling CPU vs CUDA on the same sample
- [ ] decide whether Docling belongs in:
  - OCR-required path only
  - weak/empty parse fallback path
  - relationship / LLM-enrichment path
  - a combination of the above

### Phase 4. Complex table handling

- [ ] add a second table extractor for native PDFs (`Camelot` or `tabula-py`)
- [ ] add page-level structure routing for scanned tables vs native tables
- [ ] use OCR/layout output to recover table rows before parser-profile extraction

### Phase 5. Validation

- [ ] validate OCR-derived rates against known modern sheets
- [ ] manual spot-check critical leaves and riders
- [ ] measure false-positive family matching after OCR text enters the pipeline
- [ ] document OCR accuracy by document class and backend
- [ ] document Docling extraction quality by document class and accelerator

## Heuristic Routing Guidance

Use triage output as policy, not just metadata:

- `OCR_REQUIRED` + low complexity:
  - CPU OCR queue
- `OCR_REQUIRED` + `gpu_ocr_candidate`:
  - GPU OCR queue
- high structure complexity but not scanned:
  - keep on CPU, but route to stronger table/layout extraction
- low confidence after OCR:
  - manual review or LLM-assisted extraction, not silent ingest

## Parallelism Guidance

Keep most of the pipeline on CPU.

- thread pools are acceptable for I/O-heavy PDF open/read steps
- OCR and image-heavy work should use process-based workers
- SQLite writes should remain serialized or tightly bounded
- do not parallelize inside and outside OCR backends redundantly

## Success Criteria

- OCR is no longer a dead-end route
- scanned PDFs can re-enter the normal page-mining and parser pipeline
- GPU use is selective and justified, not global
- expensive OCR work is cached and auditable
- new parsing rules land in parser profiles rather than a generic regex pile
