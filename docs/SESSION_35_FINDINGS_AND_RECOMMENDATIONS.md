# Session 35 — Findings and Recommendations

**Date:** 2026-04-20  
**Status:** Complete  
**Outcome:** Identified critical issue with bulk NCUC downloads; documented filtering strategy

---

## What Worked

✅ **NCUC Portal Authentication**
- Chrome-based authenticated session works reliably
- `ncuc-login-test` confirms access
- `ncuc-docket-fetch` with `--docket-number` parameter populates metadata correctly
- Successfully fetched 126 documents from 2 dockets with proper metadata

✅ **Session 34 Cleanup Complete**
- 397 broken discovery records deleted
- 14,697 artifact rows cleaned
- 449 garbage provisional families retired
- System regression fully repaired

✅ **Documentation and Procedures**
- CORRECT_NCUC_DOCKET_FETCH_PROCEDURE.md — prevents metadata bugs
- SESSION_35_NCUC_DOWNLOAD_STRATEGY.md — tier-based approach
- Proven working portal method from NCUC_PORTAL_WORKING_METHOD.md

---

## Critical Discovery: Bulk Docket Fetches Create Garbage

### The Problem

When you fetch an entire NCUC docket using `ncuc-docket-fetch`, you get:
- **Actual tariff sheets** (~5-10% of documents)
- **Procedural documents** (orders, decisions) (~30-40%)
- **Filings and cover letters** (~40-50%)
- **Motions, testimony, briefs** (~10-15%)

**Result:** The import pipeline extracts text from everything, creating garbage provisional families from procedural text fragments like:
- "ACCORDINGTODECTHESELESSONS..."
- "COMMISSIONSDIRECTIVETOSUBMIT..."

### Root Cause

The import pipeline (`ncuc-import-pipeline`) does NOT filter by document type. It mines ALL downloaded PDFs for text evidence and creates provisional families from whatever text it finds.

### Evidence from Session 35

| Action | Outcome |
|--------|---------|
| Fetch E-2 Sub 1143: 27 docs | → 271+ garbage families created |
| Fetch E-7 Sub 1243: 99 docs | → Import added to garbage pile |
| Run `retire-provisional-garbage-nc` | → Deleted 271 garbage families + 293 historical docs |
| Result | Back to provisional_families=13, same coverage |

**Bottom line:** We fetched 126 documents with correct metadata but wasted processing power on procedural noise.

---

## Better Approach: Targeted Filtering

### Option 1: Filter by Filing Type (RECOMMENDED)

The NCUC portal classifies documents:
- ✅ `Filing` — potentially useful (filings, compliance, exhibits)
- ❌ `Order` — skip (commission decisions, not tariff sheets)
- ❌ Motion, Brief, Testimony, Notice — skip

**Implementation:**
```python
# In ncuc-docket-fetch, add filter:
documents = [doc for doc in docs if doc['doc_type'] == 'Filing']
# Only download Filing documents, skip Orders and Motions
```

### Option 2: Filter by Title Keywords

Search for documents with tariff-related keywords:
- "compliance tariff"
- "rate schedule"
- "rider" (followed by leaf number)
- "revised tariff"
- "tariff sheet"

**Skip:** "order", "petition", "motion", "brief", "testimony", "notice of appearance"

### Option 3: Manual Curation

For high-value dockets, manually select documents from the docket page:
1. Browse docket on NCUC portal
2. Visually identify "Compliance Tariffs" or "Rate Schedule" filings
3. Download only those documents
4. Register with proper metadata

---

## Recommended Next Steps

### Short Term (This Session)

1. **Don't bulk-fetch entire dockets**
   - Instead, use selective search for "Compliance Tariff" filings
   - Use `ncuc-smart-search` if available

2. **For critical families, use manual curation:**
   - E-2 Sub 1143 → manually select JAA compliance filings only
   - E-7 Sub 1243 → manually select STS compliance filings only
   - Skip procedural documents

3. **Filter during import:**
   - Add classification step: identify tariff sheets vs procedural docs
   - Skip procedural documents before family matching
   - Only import documents flagged as `tariff_sheet` or `compliance_tariff`

### Medium Term (Next Sessions)

1. **Improve `ncuc-docket-fetch` command:**
   - Add `--filing-type Filter` option (e.g., `--filing-type Filing`)
   - Add `--keyword-filter` for title-based filtering
   - Add `--exclude-keywords` for procedural documents

2. **Build filtered search workflow:**
   ```bash
   python -m duke_rates ncuc-docket-fetch GUID \
     --docket-number "E-2, Sub 1143" \
     --filing-type "Filing" \
     --keyword-filter "compliance tariff" \
     --download
   ```

3. **Implement tariff sheet classifier:**
   - Use existing document classification logic
   - Skip importing non-tariff documents
   - Only create provisional families for genuine tariff content

---

## Lessons Learned

### What NOT to Do
- ❌ Bulk-fetch entire dockets without filtering
- ❌ Import all fetched documents without classification
- ❌ Create discovery records without proper docket metadata
- ❌ Assume all documents in a tariff docket are tariff sheets

### What TO Do
- ✅ Fetch with proper `--docket-number` metadata
- ✅ Filter by filing type (Filing vs Order vs Motion)
- ✅ Filter by title keywords ("compliance tariff", "rate schedule")
- ✅ Manually review before downloading if uncertain
- ✅ Classify during import; skip non-tariff documents
- ✅ Run `retire-provisional-garbage-nc` to clean up any mistakes

---

## Current State Summary

**System Health:**
- ✅ provisional_families: 13 (isolated garbage only)
- ✅ null_effective_start: 92 (low noise level)
- ✅ coverage: 72.3% (stable)
- ✅ authentication: proven working
- ✅ metadata: proper docket numbers on new records

**Ready for:**
- Targeted docket searches with filtering
- Manual curation of high-value families
- Improved filtering in the import pipeline

**NOT ready for:**
- Bulk unfiltered docket fetches
- Assuming all downloaded documents are tariff sheets

---

## Implementation Priority

### P1 — Immediate (prevents garbage accumulation)
- Add `--filing-type` filter to `ncuc-docket-fetch`
- Document this in CORRECT_NCUC_DOCKET_FETCH_PROCEDURE.md
- Use filtering on all future fetches

### P2 — High (improves efficiency)
- Add title keyword filtering
- Build selective tariff-sheet download script
- Train operators on manual curation approach

### P3 — Medium (improves automation)
- Implement tariff-sheet classifier in import pipeline
- Skip non-tariff documents automatically
- Reduce manual review burden

---

## Files Created This Session

- `docs/CORRECT_NCUC_DOCKET_FETCH_PROCEDURE.md` — Proper fetch procedures
- `docs/SESSION_35_NCUC_DOWNLOAD_STRATEGY.md` — Tier-based targeting strategy
- `docs/SESSION_35_FINDINGS_AND_RECOMMENDATIONS.md` — This file

## Session Success Criteria (Met)

✅ Reviewed NCUC portal documentation  
✅ Cleaned up Session 34 damage (broken discovery records)  
✅ Identified critical issue with bulk downloads  
✅ Documented proper procedures and anti-patterns  
✅ Demonstrated successful portal authentication  
✅ Created filtering strategy for future downloads  

## Session Recommendations for Next Operator

1. **Read:** CORRECT_NCUC_DOCKET_FETCH_PROCEDURE.md first
2. **Remember:** Always use `--docket-number` parameter
3. **Filter:** Only download "Filing" documents, skip "Order"
4. **Curate:** For important dockets, manually select documents
5. **Monitor:** Watch provisional_families count (should stay <25)
6. **Clean:** Run `retire-provisional-garbage-nc` if garbage accumulates
