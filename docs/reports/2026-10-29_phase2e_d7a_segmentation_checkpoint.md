# D7A — Large-document segmentation and routing checkpoint

Base: D6 local commit `2555e98`. This is a **partial D7 checkpoint**, selected
when the user requested a natural stopping point, PR publication, merge and a
complete session handoff. Adaptive bounded conversion and the rest of D7 are
not implemented by this checkpoint. D8–D10 remain unfinished.

## Changes

The existing PageEvidence/TariffSpan/segment_document pipeline is retained.
Formal sheet decisions now rely on the sheet's own header and contextual
structure instead of rate vocabulary in legal prose or calculation exhibits.
Non-tariff transitions end a tariff span. Body leaf references cannot merge
adjacent components with different own leaf identities. Dense headerless credit
appendices can retain their preceding leaf ownership. Noncontiguous original
pages stay separate; duplicate, nonpositive or unordered page identities fail.
Feature-only callers retain compatibility behavior; the stricter source rules
require page text.

TariffSpan gains a separate `segmentation_evidence` field. Boundary reasons,
page kinds, header quotes and machine attribution persist in the existing span
artifact JSON metadata. Family score data remains separate. Span cache version
advances from segmentation_v8 to v9; existing v8 records remain untouched in
this session. Page-cache round trips preserve redline, dual-rate and index
flags. Older metadata recovers those text signals from cached text, without
rerendering or OCR. This does not convert text markers into proof of redlining.

Both native and pre-mined importer entry points reject non-tariff spans before
family binding, even when incidental leaf/code references match a known family.
Related documents remain research material. This is a code guard, not a change
to existing historical document bindings, rates or regulatory status.

Triage now divides sampled text by the number of sampled pages. Previously it
compared five pages of text to the total filing page count, causing large
readable PDFs to be routed to full OCR. The 598-page appeal and 1,097-page DSM/EE
filings now receive manual-review routing instead of OCR_REQUIRED. That avoids
an unjustified whole-file route; it is not a measured end-to-end OCR speedup.

## Source reference and attribution

`bench/segmentation/d7_source_reference.yaml` covers four actual filings:
110-page JAA, 316-page tariff book, 598-page appeal record and 1,097-page DSM/EE.
Original bytes, page counts and archive aliases are checked before scoring.
The reference was frozen before segmentation predictions were generated:
Working-file SHA-256 `e537114f799f864d7e18219f44cad67d87b49eaf29784ed072b30684f1157c04`
uses Windows CRLF bytes. `reference_hash_proof.json` also records the LF-normalized
Git blob hash; no reference labels changed between the compared runs.

It contains 104 formal tariff components / 317 tariff pages among 2,121 pages.
Labels are assistant review of original PDF text and rendered source pages,
not independently human-verified or held out. No stored spans or section
predictions were used as labels. The scope is formal tariff-component boundaries
and coarse non-tariff controls, not every internal legal appendix boundary.
Rendered component-start headers and selected full boundary pages were checked;
this is not a claim that every page was visually transcribed.

Specific source findings:

- JAA pages 10–16 are an application, pages 17–21 an exhibit index, and pages
  22–110 a calculation workbook. Page 14 includes requested rates. These are
  useful research evidence, not formal tariff sheets. Existing bindings are not
  rewritten; future supporting-calculation extraction remains separate work.
- The tariff book has 103 components, including PPBE appendices on pages
  120–125 owned by the component beginning on page 117. Two distinct components
  actually print Leaf 605 (CPRE and Net Metering Bridge). The latter is not
  silently corrected to the index's different leaf identity. Source ambiguity
  remains explicit. The RDM 608 header variant is also retained.
- DSM/EE pages 1060–1062 are the three-page Rider EE form, Miller Exhibit 7.
  The printed order date is blank. The reviewed form looks clean; that does
  not establish approval. The following pages are calculation exhibits, not
  fourteen formal tariff pages as an earlier exploratory estimate suggested.
- DSM/EE pages 7–12 have no native text and were visually reviewed: appearance
  slips. The appeal's two archived paths are byte-identical; both are preserved.

## Measured results

| Input | Before | After |
| --- | --- | --- |
| Native tariff book components, IoU >= 0.9 | 89/103 | 103/103 |
| Cached artifact tariff book components, IoU >= 0.9 | 77/103 | 97/103 |
| DSM/EE formal tariff component, either input | 0/1 | 1/1 |
| False tariff pages across the four filings, either input | 1,804 | 0 |
| Formal tariff page recall, native after | — | 317/317 |
| Formal tariff page recall, cached artifact after | — | 317/317 |

The remaining six cached-artifact boundary misses are important: page-level
coverage is not proof that component ownership is correct. No population-wide
accuracy claim follows from this small development reference. The six cases
need native evidence reconciliation or review before binding/remediation.

The source-backed benchmark performs no PDF rendering, OCR, Docling model
conversion or database writes. Reference preparation did render source images.
It uses the existing production page-artifact loader for cached inputs, matching
actual source SHA-256 (or MD5 where appropriate) and complete original page
numbers. Missing/incomplete artifacts do not receive an empty successful score.

Measured peak process working-set/RSS proxy: 299.9 MiB in the recorded after
run. On the tariff book, native feature collection took 2.67 seconds and
segmentation 0.080 seconds; cached input collection took 1.43 seconds and
segmentation 0.101 seconds. Concurrent workloads and machine state limit timing
comparability. No GPU conversion ran; VRAM is explicitly not measured.

All 12 billing records and the summary exactly match D6: 11 good matches, one
needs review, maximum absolute difference $0.45. No numerical parser, rate
profile or billing calculation changed.

## Validation and scope

Thirteen new checkpoint regression cases cover own-header ownership, legal and
exhibit negatives, type transitions, nonbreaking-space table continuation,
missing/duplicate page ownership, cache metadata recovery, sampled routing and
both import entry points. Existing targeted integration tests also pass.
Final full suite: **1,998 passed, 92 skipped, four xfailed**, 135 existing
warnings, 606.04 seconds. A first full run had one outdated version fixture;
its current-row setup now uses SPAN_ARTIFACT_VERSION and the stale-row assertion
is preserved. The final rerun passes. The checkpoint's 13 new regression cases
also pass separately. New benchmark/test files pass Ruff and the staged diff
passes whitespace checks. GitNexus reports HIGH aggregate staged risk, with
47 symbols and six reported affected flows before final report packaging;
AST review confirms nine changed production functions/methods, one test fixture
and one added data-model field. No numerical or billing file changed. Interrupted earlier runs remain on disk and are not counted as
passing final validation.

GitNexus impact was reported before changes. Shared classifier/segmenter/triage
and page/span cache functions carry HIGH or CRITICAL impact (up to six direct
callers / five affected processes); the span model has 16 direct uses. Both
importer guards have narrow LOW method impact. Graph flow-budget limits are
supplemented by AST/direct caller review. Unrelated importer class formatting
was removed; the importer changes are only the two guards.

No production, archive, accepted charge, effective-date or regulatory-status
writes were made in D7A. Production hash/mtime are checked against the recorded
post-D4-incident state. The overall original production-preservation gate
remains **FAILED** because of the D4 incident; see the separate incident report.

## Reproduce

```powershell
.venv312/Scripts/python.exe -m pytest tests/test_d7_segmentation_checkpoint.py -q
.venv312/Scripts/python.exe bench/score_segmentation.py --db scratch/d6.db `
  --out reports/phase2e_d7/segmentation_after.json
.venv312/Scripts/python.exe bench/billing_check.py --src src --db scratch/d6.db `
  --label d7_checkpoint --read-only --out reports/phase2e_d7/billing_after.json
```

The source PDFs must match the reference manifest. The benchmark fails on
missing/changed source evidence instead of reporting unsupported scores.

## Resume point

Finish D7 bounded extraction: strict partial-conversion checks, adaptive chunks,
safe overlap, unique original page/table ownership, table continuation and
representation-cache identity. Reconcile the six cached boundary cases and
extend independent negative/held-out source labels. Then proceed to D8 model
configuration benchmarking, D9 candidate-only historical reconstruction and
D10 label/training readiness. Nothing here authorizes production data promotion.
