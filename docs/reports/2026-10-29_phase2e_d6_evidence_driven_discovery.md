# Phase 2E D6 — Evidence-driven document discovery

Research implementation on `codex/d6-evidence-driven-discovery`, based on local
D5 commit `7dc5caa`. No push, merge, live network request, live PDF download, rate
promotion, historical binding, effective-date change, or production write.

The D4 preflight incident remains an **overall production-preservation failure**.
D6's final file hash/mtime match the recorded post-incident state; that does not
establish preservation of the original pre-D4 database or reverse the incident.
See [incident report](2026-10-29_d4_preflight_safety_incident.md).

## Result and limits

D6 extends the existing clue, search, candidate, lineage, and blob registries.
It adds **185 local candidate records across 70 existing gaps**, all partial
matches requiring source review. Net gaps having candidates increase from 206
to 212. Existing candidate records increase from 628 to 813. Full matches fall
from 87 to 84: three previous full candidates have insufficient readable own-page
evidence and are correctly downgraded. No candidate becomes an official tariff.

The archive still has 691 active typed gaps. Sixteen prior verified/resolved gap
states are preserved; the remaining 675 have not been resolved. The 45 existing
machine-verified supersession links remain. These counts do not establish
independent human verification, new approval scope, or numerical correctness.
There are no newly retrieved live documents and no newly verified approval
relationships. Newly recovered candidates are pointers into the existing archive.

Detailed records: `reports/phase2e_d6/candidate_audit.json` and the reproducible
`discovery_benchmark_final.json` (large JSON payloads may be stored as `.gz`).

## Existing infrastructure and actual gaps

Baseline read-only inventory: 30,027 clues, 25,085 associations, 6,084 scanned
pages, 201 version links, 691 typed gaps, 628 local candidates, 1,120 search
attempts, 1,419 remote candidates, 248 review items, and one registered blob.
There are 153,911 native artifact rows, about 294 MB of cached native text,
4,615 distinct artifact source paths, and 730 successfully fetched order records.
Existing discovery/lineage baseline: 62 tests passed.

The legacy extractor already retains docket numbers, dates, revisions,
supersession, leaves, schedules and approval references. Existing clues have
literal page-text spans, hashes, optional geometry and separate normalization.
D6 preserves the original extractor's default identities and 50-page benchmark.
The missing pieces were durable own-page multi-docket evidence, evidence-aware
negative caching, safe file reuse, and document-level relationships outside the
version-only supersession table.

## Implementation

### Local-first search and cache validity

`archive_state` now hashes native artifact contents and identity metadata plus
order discovery records. It includes extractor, redline, reference and coverage
method versions, and source file size/mtime/availability fingerprints. The
planner computes the snapshot once per run. This is a cache invalidation token;
it is not proof that original bytes are intact. Candidate reads separately
compute actual SHA-256. Changes that preserve both file size and nanosecond mtime
are outside the file-stat invalidation guarantee.

The native index selects the newest artifact per source path, page **and stored
file hash**, including the latest artifact ID on a version tie. A stale artifact
for another file identity cannot hide valid current-file text. Search hits must
then carry the expected revision in verified current-file page text. The index
is deliberately overinclusive; integrity checks reject stale identities before
advancing a candidate. It is constructed only if an uncached page search needs it.

Unavailable PDFs, invalid page integrity and missing native page evidence produce
`incomplete`, with a stop reason. Known bounded coverage limits can be reused on
an unchanged snapshot without being relabeled `no_results`; errors, interrupted
attempts and blocked searches remain retryable. Explicit `force` permits a retry.
Network planning accepts a current, explicitly limited local attempt as a reason
for targeted fallback, while recording `local_search_complete=false`.

A full tariff candidate now requires clean redline evidence on every requested
page, verified page text, the expected identity, utility and effective window.
Candidate IDs and review columns remain stable. Candidates 387, 389 and 527
change from full to partial because their page verdict is `no_text_layer`.

### Multi-docket source evidence

`document_references` reads hash-matching cached native order pages and falls back
to the first two original PDF pages. It retains source SHA-256, exact page,
printed quote, offsets, role, geometry when uniquely located, producer version
and discovery-record IDs. Caption/body roles are machine interpretations of
literal source facts. Portal filing docket metadata remains separate.

A bounded caption read is not a complete whole-document negative. Missing PDFs,
unreadable sources, unscanned body pages and blank native pages retain coverage
limits. One PDF is held open at a time for geometry. The existing
`order_caption_index` API remains compatible; the resolver uses the richer index.
No PDF rendering or OCR is added.

The research run adds 4,316 reference-producer observations. Several paths can
identify identical PDF bytes; candidate deduplication remains by gap and SHA-256.
An evidence path may be another byte-identical archived alias, with its own
original filing-record IDs preserved. It is never evidence from a different hash.
For example, existing order evidence filed under E-2 Sub 1275 explicitly prints
`DOCKET NO. E-7, SUB 1026` on page 1, with a located bounding box. This locates a
reference candidate for the E-7 gap; it does not establish that this is the cited
2013 order or that it approves the target tariff.

### Relationships without a competing graph

Schema version 6 adds only `document_relationships`. Existing constrained evidence
tables are not rebuilt or widened. `lineage_links` continues to own version
supersession. The new relational extension expresses `proposed_revision_of`,
`approved_by`, `implements`, `references`, and `clean_counterpart_of`, with source
identities, supporting clue IDs, target descriptions, machine checks, builder
version and independent review columns.

Approval and implementation claims remain source assertions requiring scope
review. Docket agreement alone cannot verify approval. Clean-counterpart records
remain inferred candidates. Obsolete relationship records are retained with
inactive machine state, and rebuilds preserve prior reviews and supporting
occurrences. The schema usage/drop guard includes reviewed relationships.

A proposed header cannot produce verified actual supersession, either as the
successor or as an eligible predecessor. A printed proposal reference is retained
as a proposed relationship. The process-global order-existence cache is removed;
queries now reflect the actual connection and current catalog.

### Durable retrieval

The existing storage module now validates PDF readability and SHA-256, stages
exact original bytes, and atomically publishes a hash-addressed file beneath the
explicit archive root. It verifies existing blobs and refuses conflicting bytes
instead of overwriting evidence. URL deduplication rechecks actual cached files.
Downloaded candidates point to durable archive paths and register in the existing
`document_blobs` table. Interrupted/invalid incoming files remain available for
diagnosis. Linking rechecks the archived bytes before accepting a named review.
The existing CLI defaults to `data/historical/ncuc/remote_candidates`, outside
scratch. These paths are covered with fixture downloads; no live PDF was needed.

## Measurements

Initial complete local pass: 370 searches, 84 full and 723 partial match
occurrences; duplicates can serve multiple gaps. These are search occurrences,
not 807 distinct newly found documents. Unique candidate totals are above.
The first pass took about 144 seconds under concurrent validation. Repeated known
coverage originally caused 128 redundant attempts; the corrected cache avoids
all 370. Lazy indexing reduces an unchanged repeat from about 43 seconds to about
12 seconds, with zero page-index builds. These are concurrent-run measurements,
not isolated CPU speed claims. Final reproducible timings are in the benchmark.

The original network planner produced 245 distinct queries (58 order inventory,
134 public search, 53 parameter search). The revised local findings and explicit
limited-coverage fallback produce 241 (54, 134, 53). This is a query plan, not a
count of HTTP requests: paged inventory requests vary. No planned live query was
executed. Some gaps still lack a locally searchable target and remain explicit.

Existing D4 six-page captured inventory was replayed offline: 568 entries,
83 public order candidates, and a repeated completed query avoided. This is reuse
of an earlier verified capture, not a fresh current portal inventory. It adds no
new discovery claim and makes zero network requests.

The 50-page clue benchmark is exactly unchanged: revision 59 TP / 0 FP / 2 FN;
supersession 27 / 0 / 1; effective date 35 / 1 / 2; docket 45 / 0 / 0; order date
33 / 0 / 1. No source evidence failures; seven discrepancies remain. The sample
was transcribed by Claude from rendered pages. It is assistant-created evidence,
not independent human labels or a held-out accuracy study.

All 12 read-only billing records and their summary match committed D5 output
exactly: 11 good matches, one needs-review case, maximum absolute difference
$0.45. Canonical table hashes and prior review columns match the preserved D5
snapshot, including the 248-item review queue. No numerical extraction or billing
calculation was changed.

## Validation and reproduction

Full suite: **1,983 passed, 92 skipped, four xfailed**, 135 warnings, 718.51
seconds. Final focused suite: **78 passed**, including all 16 D6 cases. Full-suite
collection preceded the final two edge-case tests and coverage/version/wording
polish; the final focused run validates those additions and the final source.
New source files, benchmark and tests pass Ruff; staged whitespace check passes.
The 50-page and 12-bill comparisons above are exact matches. Earlier interrupted
full runs are retained and are not counted as passing validation.
The D6-specific tests currently cover stale in-place evidence, unavailable/blank
sources, bounded fallback, unchanged-cache indexing, multi-docket exact spans,
proposal authority, additive v5 upgrade, durable deduplication, archive conflicts,
source mutation, review preservation and production/hardlink refusal.

```powershell
.venv312/Scripts/python.exe -m pytest -q
.venv312/Scripts/python.exe -m pytest tests/test_d6_evidence_discovery.py -q
.venv312/Scripts/python.exe bench/ncuc_discovery_evidence.py `
  --db scratch/d6.db --baseline scratch/d5.db --apply `
  --out reports/phase2e_d6/discovery_benchmark_final.json
.venv312/Scripts/python.exe bench/clues/score_clues.py `
  --db scratch/d6.db --out reports/phase2e_d6/clue_score_after.json
.venv312/Scripts/python.exe bench/ncuc_inventory_replay.py `
  --out reports/phase2e_d6/inventory_reuse_benchmark.json --repetitions 3
.venv312/Scripts/python.exe bench/billing_check.py --src src --db scratch/d6.db `
  --label d6 --read-only --out reports/phase2e_d6/billing_after.json
```

Create the research target as an explicit copy before `--apply`; the baseline
must be a different preserved snapshot. The benchmark refuses writable production
paths and hardlink aliases before opening them. It makes no network requests.
Earlier interrupted full-suite logs and intermediate research results are retained;
only completed final checks count as validation.

GitNexus context/impact checks precede existing-symbol edits. HIGH risk was
reported for the shared archive-state/clue-identity/writer functions (up to three
flows) and the new shared reference extractor (two direct callers/two processes).
Schema installer and relationship builder impacts were LOW. CLI/constant/new
unindexed symbols can report UNKNOWN; absence of callers was not treated as proof
of isolation. The final graph has 20,589 nodes, 40,026 edges and 972 reported flows. Staged
change detection finds 67 symbols and 30 expected affected flows, aggregate
CRITICAL risk, with no tool error or partial result. The AST audit finds exactly
18 existing functions changed; no numerical parser/billing files, databases,
archive bytes, models or dependency installs are staged. This reviewed scope is
limited to the documented discovery/lineage/storage paths and additive schema
extension. The index reports a global execution-flow budget and partial
FTS label availability; AST/direct wiring review supplements the graph.
