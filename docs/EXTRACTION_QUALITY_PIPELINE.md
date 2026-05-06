# Extraction Quality Pipeline: Docling → Fingerprint → Ingest

**Purpose:** Efficiently process 610 tariff sheets while filtering out redlines and low-quality documents.

**Status:** Legacy workflow note. This document describes an older Docling/HQ
ingest path built around `ingest-ncuc`. It is not the default sanctioned
historical pipeline. Prefer
[document_parsing_pipeline_guide.md](/c:/Python/Duke/Standalone/docs/document_parsing_pipeline_guide.md)
for current operator workflows.

---

## Pipeline Overview

```
PHASE 1: Docling Batch (GPU)
    ↓ (30-60 min)
PHASE 2a: Fingerprinting (Quality Assessment)
    ↓ (2-3 min)
    ↓ → Detects: redlines, dual-rates, scanned/OCR, poor confidence
    ↓ → Classifies: HQ | UNCERTAIN | REDLINE | SCANNED
    ↓
PHASE 2b: Charge Extraction (CPU/GPU)
    ↓ (20-30 min)
    ↓ → Ingests HQ documents only
    ↓ → Skips redline candidates
    ↓
PHASE 3: Validation & Summary
    ↓ (5-10 min)
    ↓ → Final charge counts
    ↓ → Gap analysis
    ↓
Decision Point: Tier 2 (historical) or declare success?
```

---

## Execution: Quick Start

### Automated (Recommended)

When Phase 1 completes, run:
```bash
python scripts/maintenance/run_full_extraction_pipeline.py --phase2-only
```

This runs all phases automatically with:
- ✓ Quality fingerprinting
- ✓ Redline detection
- ✓ Charge extraction
- ✓ Validation
- ✓ Summary report

**Duration:** 1-1.5 hours total (including all three phases)

### Manual Control

If you want to inspect between phases:

```bash
# Phase 2a: Fingerprint and assess quality
python scripts/analysis/fingerprint_docling_artifacts.py

# Phase 2b: Extract charges
python -m duke_rates ingest-ncuc --persist --replace

# Phase 3: Validate
python scripts/debug/final_charge_summary.py
python scripts/analysis/analyze_dep_gap_impact.py
```

---

## Quality Fingerprinting: What Gets Detected

### Redline Signals (Recommendation: REVIEW)
- ✓ Dual-rate patterns (0.0464/0.0512) — strongest indicator
- ✓ Keywords: DRAFT, PROPOSED, REDLINE, NEW, OLD
- ✓ High redline_confidence (>0.5)
- → These documents are excluded from charge extraction

### Scanned/OCR Documents (Recommendation: REVIEW)
- ✓ Tesseract OCR was used (poor quality indicator)
- ✓ OCR artifacts detected in text (???, ------, etc.)
- → Can be ingested but flagged for manual review

### Uncertain Quality (Recommendation: REVIEW)
- ✓ Conversion confidence < 0.7
- → Ingested but tracked separately

### High-Quality Documents (Recommendation: INGEST)
- ✓ No redline markers
- ✓ No dual-rate patterns
- ✓ Native PDF text (no OCR)
- ✓ Confidence > 0.7
- → These are ingested into tariff_charges

---

## Expected Results

**Before Pipeline:**
- 42,389 charges in database
- 188/1,073 versions with extracted charges (18%)

**After Phase 1 (Docling):**
- 610 documents converted to structured format

**After Phase 2a (Fingerprinting):**
- ~550-580 HQ documents identified (90%+)
- ~20-30 redline candidates flagged
- ~10-20 scanned/uncertain documents noted

**After Phase 2b (Extraction):**
- 300-500 new charges extracted from HQ documents
- Coverage improves: 188/1,073 → 400+/1,073 (40%+)
- Most families improve: leaf-602, 605, 606, 607, 608, 609

**After Phase 3 (Validation):**
- Charge counts by family
- Gap analysis showing remaining work
- Ready for Tier 2 decision (historical vs. parsing improvements)

---

## What Gets Skipped/Flagged

### Redline Candidates
Documents detected as redlines are **NOT ingested** into tariff_charges to avoid false rate extraction.

Stored separately for:
- Manual review and validation
- Version comparison (official vs. draft)
- Historical analysis

### Scanned Documents
Documents requiring full-page OCR are flagged but can still be ingested (with quality tracking).

---

## Database Changes

**New fingerprinting data stored:**
- Quality tier classification (HQ/UNCERTAIN/REDLINE/SCANNED)
- Redline confidence scores
- Conversion confidence
- OCR usage indicators
- Document quality summary

**Charge table:** Only HQ documents contribute to tariff_charges

---

## Monitoring & Troubleshooting

### Check Phase 1 Progress
```bash
tail -f docling_batch_processing.log
# Look for: [N/610] and time per document
```

### Check Phase 2a Results
```bash
python scripts/analysis/fingerprint_docling_artifacts.py
# Shows: HQ count, redline detections, quality breakdown
```

### Check Final Results
```bash
python scripts/debug/final_charge_summary.py
# Shows: new charges by family, coverage improvement
```

---

## Timeline Estimate

| Phase | Duration | Status |
|-------|----------|--------|
| Phase 1 (Docling) | 30-60 min | Running (currently 156/610) |
| Phase 2a (Fingerprint) | 2-3 min | Awaiting Phase 1 |
| Phase 2b (Extract) | 20-30 min | Awaiting Phase 2a |
| Phase 3 (Validate) | 5-10 min | Awaiting Phase 2b |
| **Total** | **60-110 min** | **~1-2 hours** |

---

## Next Decision

After Phase 3, review coverage improvement:

- **Coverage ≥ 40%?** → Success! Consider Tier 2 (historical gaps) or declare done.
- **Coverage < 40%?** → Proceed to Tier 3 (parsing improvements).

---

**Status:** Phase 1 running. Fingerprinting pipeline ready. Will execute automatically when Phase 1 completes.
