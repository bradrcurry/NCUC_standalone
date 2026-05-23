# Next Session Priorities
**Date Updated:** 2026-04-26 (Session 43)
**Purpose:** Active priorities only; pair with `NEXT_SESSION_START_HERE.md`

## Active Priorities

1. **Carolinas remaining unknown-profile families: SCG text-bound, PS bootstrapped, PPBE unresolved**
Why it matters:
The `unknown` profile bucket still has significant volume. SCG profile works (confirmed matching hd=477) but 4 of 5 docs lack OCR text. PS has 3 bootstrapped docs with charges preserved but no profile matching. PPBE has 2 docs (1 with text, heuristic_parser "parsed").

Next action:
- Get OCR text to remaining SCG/PS docs (Docling structure-sensitive lane)
- Check if PS profile is worth creating vs accepting bootstrapped charges

Done condition:
- SCG docs matching profile; PS/PPBE categorized or accepted with documented rationale

2. **DEP leaf-521 and leaf-501 empty latest processing runs**
Why it matters:
hd=2811 (leaf-521 SGS-TOUE, Span 1-3) and hd=3199 (leaf-501 R-TOUD, Span 1-4) have `latest_run_status=empty` despite stored charges. Recent reprocesses returned empty — needs investigation.

Next action:
- Enqueue hd=2811 and hd=3199 and check what the reprocess returns; inspect why profile returns empty for these docs

Done condition:
- leaf-521 and leaf-501 `weak_latest_parse` anomalies resolved or diagnosed

3. **DEP pre-2014 residential gap (accepted caveat, low priority)**
RES-24/25/26 for leaf-500 not found in local Sub 1023 data. Gap 2012-12-01 → 2014-06-01 remains as accepted caveat unless portal search yields new evidence.

4. **Docling structure lane blocked — `process-docling-batch --ocr-remediation` finds 0 matches**
Docs flagged for `run_docling_or_paddle_structure` have page artifacts from prior CPU Docling runs, so the cache filter excludes them. Their `raw_text_path` is still NULL. Need to investigate the filter logic or force-reprocess.

## Session 43 Completed
- OCR backlog workflow completed steps 1 (107 enqueued) and 2 (110 drained, 0 failures)
- 3 stuck OCR items (running since Apr 24) reset and processed successfully
- Extraction step 3 in progress: ~287 docs processed, +1,619 new charges
- SCG profile confirmed working (2 charges, quality=strong on hd=477)
- Bulk extraction produced 1,490 new charges across strong profile matches
- Session handoff docs updated
- Coverage: 647/879 (73.6%), charges: 14,890

## Current Accepted Caveats

- Some `needs_review` volume still comes from older backlog cohorts and should not be confused with new pipeline regressions.
- Some rider or formula sheets are correctly expected-zero or single-value outputs and should not be treated as parser failures by default.
- DEP bundle-era structural richness is still narrower than full multi-class historical completeness.
- DEP leaf-500 (RES) gap 2012-12-01 → 2014-06-01: RES-24/25/26 not found in local Sub 1023 data. Service regulations and corrections filing are present but not the rate schedule itself.
- Carolinas RS hd=1975 (Span 4-5): 4-column multi-category table, only 1 charge extractable with current parser.
- Carolinas LGS hd=2609 (Span 31-31): `skipped_procedural` — page 31 may be a non-tariff exhibit page.

## Session Start Commands

```powershell
python -m duke_rates show-workflow-status-nc
python -m duke_rates parse-review-summary
python -m duke_rates reprocess show-priority-nc
python -m duke_rates export-nc-anomaly-audit
python -m duke_rates ocr report-benchmark-nc --sort-by weak-first
```

## Not For This File

Do not expand this file into:
- a session-by-session accomplishment log
- a duplicate of `roadmap.md`
- a long historical caveat inventory
- a replacement for generated audits

Put that material in reports, roadmap, or the relevant canonical workflow doc.

**Status:** Active
**Update Trigger:** Refresh when the top active work items or their next actions change.
