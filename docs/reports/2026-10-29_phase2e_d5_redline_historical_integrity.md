# Phase 2E D5 — native redline evidence and historical integrity

Date: 2026-10-29. Branch: `codex/d5-redline-historical-integrity`, based on
`90be8be`. Implementation, tests and scope review complete; delivery is local only.

## Outcome and authority boundary

The existing page detector now preserves native observations, geometry and
machine classifications, distinguishes common link/decoration false positives,
and recognizes additional vector markup. It performs no rendering or OCR.
Shared promotion validation refuses insufficient native evidence. The LLM
writer now checks readable bound sources, comparable archive hashes, page
bounds, document/span identity and evidence-file identity. The former direct
proposal-to-`approved` shortcut is retired; proposed-rate research is preserved.

The 29-page regression corpus improves from 11 true positives / 1 false positive
to 11 true positives / 0 false positives, with five explicit abstentions.
These are **assistant source-review labels**, not independently human-verified
ground truth, and the corpus was used during development.

No charge, effective date, approval status or production binding was corrected.
The existing review queue was rebuilt only on an explicit D3 research copy:
248 items remain open, with no human-review labels. Source exposure is a risk
indicator, not proof of an incorrect amount or actual use in a bill.

## Preservation

The [D4 preflight incident](2026-10-29_d4_preflight_safety_incident.md) remains
an acceptance failure for the overall run. A documented dry-run command opened
production through a migration-capable helper, changing its file hash and
mtime. D4 fixed that helper; this report does not retroactively claim that
production remained unchanged throughout the project.

D5 uses raw SQLite `mode=ro` plus `query_only=ON` for production audits, and
never invokes migrations there. Current production SHA-256 remains:

`0f6f8f3551f21908533019c5f568d49e254fdd0149fa65ceedef9aa37031b6ca`

Size: 5,845,692,416 bytes. Mtime: `1793255717771976700` ns. Both match the
post-incident baseline. Evidence: `reports/phase2e_d5/production_hash.json`.
No restoration, production promotion, archive deletion, push or merge occurred.

## Existing implementation and targeted changes

The detector already used `char_flags & 1` correctly. Its module docstring
incorrectly described font bit 3 as strikeout; that prose is corrected.
Character flags carry strike/underline information; font flags describe styles
such as monospacing. Reference: [PyMuPDF span dictionary](https://pymupdf.readthedocs.io/en/latest/textpage.html#span-dictionary).

`PageRedlineEvidence` and the four compatibility verdicts remain in place.
`page_redline_evidence@1.1.0` adds:

- Exact span text, original page supplied by the caller, source path, span and
  mark boxes, font/style/color, detection method and signal strength.
- Native character underlines and annotation observations. Strikeout annotation
  quads are retained separately, preventing a multi-line annotation's large
  enclosing rectangle from deleting intervening text.
- Individual vector path segments, including black strike lines crossing
  numerical tokens. Numeric coverage and line-length checks distinguish short
  rate strikes from long table rules and negative signs.
- Color/insertion corroboration on the affected span, requiring a numerical
  value or revision identity. Unrelated colored logos cannot corroborate a
  heading. Underlined email/URL spans remain ambiguous rather than tariff edits.
- Font/style and weak annotation observations remain evidence, not independent
  proof of tracked changes. Underlined colored ordinary headings are ambiguous.
- Image-dominant pages remain insufficient even with searchable text overlays:
  flattened visual edits are not represented by that overlay's native geometry.
- Strike coverage uses the union of overlapping regions; duplicate character,
  annotation and vector evidence cannot manufacture additional coverage.

Machine classifications project onto the existing verdicts:

| Classification | Compatibility verdict | Meaning |
|---|---|---|
| `confirmed_material_redline` | `redline` | A strong native deletion signal intersects a numerical value; machine interpretation |
| `likely_redline` | `redline` | Structural edit evidence, including non-numerical identity changes |
| `ambiguous_markup` | `suspect` | Weak/contextual signals warrant review |
| `no_redline_evidence_detected` | `clean` | No qualifying native signals detected |
| `insufficient_evidence` | `no_text_layer` | Native inspection cannot establish the page's visual state |

Neither `clean` nor any classifier outcome establishes regulatory approval.
Ambiguous markup is reported rather than automatically blocked by itself;
redline findings and insufficient native pages block unattended promotion.
Image-dominant pages may contain an OCR layer despite the legacy verdict name.

`summarize_page_evidence` retains legacy keys and adds evaluated/insufficient
page lists. Mixed PDFs are assessed over the requested source span; evidence
on another page does not label every page a redline.

## Source-reviewed regression benchmark

`tests/fixtures/d5_redline/manifest.json` records 29 original pages from
11 archived PDFs, original page numbers, source/fixture SHA-256 values, review
notes, label provenance and pre-D5 verdicts. New excerpts preserve native PDF
objects through `insert_pdf`; existing D2 excerpts retain their original source
provenance. The corpus includes mixed clean/marked filings, numerical and
identity edits, official-copy stamps, headings, negative values, table/box
decoration, an underlined email false positive, and five image-dominant pages.
An additional native-integrity check confirms all 29 excerpts match their
original source hashes, text/spans, drawing geometry and annotations, with
coordinates compared to 0.001 point (`fixture_native_integrity.json`).

| Measurement | Pre-D5 | D5 |
|---|---:|---:|
| True positives | 11 | 11 |
| False positives | 1 | 0 |
| False negatives on assessed pages | 0 | 0 |
| True negatives | 17 | 13 |
| Insufficient / abstained | 0 | 5 |
| Precision against assistant labels | 91.67% | 100% |
| Recall on assessed positives | 100% | 100% |
| Positive-label assessment coverage | 100% | 100% |
| Overall assessment coverage | 100% | 82.76% |
| Original page attribution | — | 29/29 |
| Insufficient-page attribution | — | 29/29 |
| Detection rendering / OCR calls | — | 0 / 0 |

Five complete runs took 2.169, 2.015, 1.966, 1.900 and 1.840 seconds; median
**1.966 seconds**. Timings were recorded during other local verification jobs;
they are not an isolated speedup comparison. Source review itself used rendered
images. Synthetic geometry controls are separate from the archived-page scores.

The sample is small and curated, has no independently verified human labels,
and was reused during development. Scanned-positive recall is not established:
image-dominant pages abstain. Pathological fonts, tiled raster overlays, unusual
geometry, and uncolored additions can still require visual review. Strength
labels are heuristic categories, not calibrated probabilities. No universal or
foolproof detection claim is made.

Reproduce without a database or network:

```text
.venv312/Scripts/python.exe bench/redline_native.py --out reports/phase2e_d5/native_benchmark_replay.json
```

Final evidence: `reports/phase2e_d5/native_benchmark_final.json`.

## Historical source exposure

A read-only baseline reproduced D3 exactly: 411 versions with linked charges,
65 flagged versions, 45 strong-evidence versions and 1,113 associated charges.
Strong uses the existing D3 rule: glued revision text or at least three struck
spans. It does not mean those charge values are proven wrong.

| Current finding | Result |
|---|---:|
| Versions with linked charges | 411 |
| Linked charge rows | 5,082 |
| Current redline-exposed versions | 57 |
| Strong current findings | 47 versions / 1,120 charges |
| Prior and current redline-finding union | 66 versions |
| Insufficient native evidence | 79 versions |
| Missing historical source | 1 version |
| Actual comparable archive-hash mismatch | 1 version, also redline-exposed |
| Distinct versions failing a shared source check | 135 |
| Utility-web versions outside historical-span checks | 27 |
| Charge rows without a matching version | 116 |

Nine prior flag triggers were removed:
4570, 5239, 5382, 5457, 5691, 5702, 5717, 5728 and 6814.
Rendered review of the changed pages identified underlined counsel email or
website references. This is not whole-span clearance: v4570 and v6814 also
contain image-dominant pages requiring review. Their prior queue findings remain
open and carry an attributed assistant note.

Newly flagged v7000 has green old/new Rider SCG revision/footer markup on original
pages 13–15. Existing flagged v5317 has stronger evidence on pilot-tariff pages
10–12. Both were inspected as rendered pages; no charge correction follows from
those observations. The original 45 strong findings remain part of the review
set. Current grade changes account for the two additional strong findings.

`strong_source_charge_review.json.gz` makes all 1,120 charge rows on these
47 versions reviewable alongside their source bindings, stored regulatory
references, lineage records and available counterpart candidates. Of those
rows, 918 snippets contain labels/other text without a numerical quote; 202
contain numerical text whose value/unit relationship is not independently
grounded. Exact per-charge page attribution is not established by this report.
It preserves version-bound spans rather than inventing a page or claiming that
a particular struck amount produced an existing charge. This is a D9/D10
provenance limitation, not proof that the numbers are wrong.

The missing historical source is v6159 / hd3114, pointing to an unavailable
pytest temporary PDF. Its cause is not established. The comparable hash
mismatch remains v6059. The 27 utility-web exceptions must not be counted as
27 missing historical files. The 116 unlinked charges remain intact for D9;
their origin and relevance are not established by this audit.

Baseline grouped inspection took 63.094 seconds; the current shared audit took
78.307 seconds with additional native and readability checks. These runs do
different work and ran amid other local jobs; no speedup claim is supported.

Reproduce the shared audit using a strictly read-only wrapper:

```text
.venv312/Scripts/python.exe bench/redline_exposure.py --db PATH --out PATH
```

It calls the existing `audit_version_sources` function, opens no migration
connection, creates no catalog or queue, and refuses to create a missing DB.
Evidence: `baseline_exposure.json`, `baseline_source_coverage.json`,
`shared_source_audit.json` and `remediation_preparation.json` under
`reports/phase2e_d5/`.

## Clean counterpart comparison and remediation queue

The existing D3 ledger contains 18 clean-counterpart candidates for 12 versions
in the prior/current exposure union. They remain candidates; cached `verified`
labels from an assistant's page-image review are not independent human approval.

Worked example: candidate 399, original pages 7–8 of
`e-7-sub-1304/b0d77287-44af-4c44-aaa3-4226369d9514.pdf`, is a clean-form
Sixty-Second Revised Leaf 99. Its recorded hash matches the current file and
fresh native inspection finds no redline evidence. Page 7 prints service
effective September 1, 2024 and references E-7 Subs 1304/1307 orders dated
August 20, 2024. That printed reference does not independently verify approval.

Three residential stored values on flagged v6737 match this source after
converting dollars to cents per kWh:

| Rider | Stored $/kWh | Printed cents/kWh |
|---|---:|---:|
| FCA | 0.014464 | 1.4464 |
| EE | 0.003775 | 0.3775 |
| Existing DSM | -0.000027 | -0.0027 |

Exact printed tokens, original page, boxes, source hash and stored row IDs are
retained in `remediation_preparation.json`. This compares three values, not all
46 charges on that version. No binding or promotion was performed.

The existing `evidence_review_items` table was rebuilt only in `scratch/d5.db`,
copied from the preserved D3 research DB. Its queue grows from 166 to **248 open
items**: one new redline finding, 79 insufficient-source findings, one missing
source finding and one D9 unlinked-charge investigation. Prior redline records
and all prior review decisions remain. Priorities total 163 at priority 1,
39 at priority 2 and 46 at priority 3. These indicate potential downstream
impact, not verified per-bill usage.

Evidence levels: **211 rule, 37 assistant page review, 0 human review**.
The legacy problem-type name `billed_charges_on_redline_source` remains for
compatibility; its explanation now describes source exposure rather than
claiming a marked amount necessarily produced a charge. Actual SHA-256 values
and legacy archive identifiers are stored separately in queue evidence.

Large evidence payloads are committed as `baseline_exposure.json.gz`,
`shared_source_audit.json.gz`, `review_queue.json.gz`,
`remediation_preparation.json.gz` and `strong_source_charge_review.json.gz` in
`reports/phase2e_d5/`. Their readable working JSON files are generated locally;
standard Python `gzip.open(path, 'rt')` reads the committed copies.

Before/after row hashes match for all nine checked tables: families, versions,
historical documents, charges, rider summary sources/blocks/items, lineage links
and typed gaps. Review construction changes queue data only. Evidence:
`review_queue.json` and `review_queue_build_summary.txt`.
The six checked authoritative tables also match production row-for-row on the
D3 research copy, as recorded in `production_scratch_authoritative_comparison.json`.

## Promotion pathways audited

| Writer / entry point | D5 boundary |
|---|---|
| `provenance promote`, direct `pv.promote` | Shared validation remains inside the writer; automated pathways cannot override failures |
| Bulk legacy replacement | Reaches the same guarded candidate/run promotion |
| Rider-summary legacy replacement | Shared validation over actual evidence pages; no inferred approval |
| LLM charge promotion and overnight job | Full source-integrity helper before inserts; readable source, verifiable archive hash, matching historical document/span and evidence bytes, valid bounds, native evidence, redline assertions/fingerprints |
| Proposed-tariff direct promotion | Retired: actionable raw plans raise before any write; private accepted-insert helpers removed; CLI `--confirm` refuses before DB settings/open |
| Proposed extraction/comparison/planning | Preserved in isolated proposal tables; redlines remain valid research inputs |
| Reviewed source binding | Existing propose/approve/apply/rollback workflow retained; binding or candidate charges do not automatically grant approval status |
| Utility-web `replace_parsed_tariff_data` | Existing historical-data-loss protection retained; separate utility-web source path, not the NCUC historical-span audit |
| Rebind/deletion helpers | Existing explicit charge-deletion protection retained; not run in production |
| Low-level/manual SQL helpers | Repository upserts, audit/manual repair helpers, inactive legacy `BulkExtractor.insert_charges`, and private promotion internals are not supported unattended ingestion entry points; they are not a substitute for the guarded workflow |

The supported unattended ingestion entry points are guarded; this is not a
claim that arbitrary scripts using low-level SQL can never bypass application
policy. No new approval system or source catalog was introduced.

`ingestion_validation@1.1.0` adds the named `bound_pages_readable` check. A
reviewed pathway can explicitly override it using the existing reviewer/reason
recording; unattended overrides remain forbidden. Legacy fingerprint findings
are retained rather than silently cleared by the new native detector.

The proposed command now opens planning DBs read-only. Moving to accepted
records requires the existing `provenance propose-binding`, `approve-proposal`,
`apply-proposal`, `extract-candidate` and reviewed `promote` workflow, with
regulatory approval reviewed separately before billing selection. A raw proposal
plan, confirmation flag or clean-looking page cannot establish approval.

## Validation and GitNexus

### Billing-validation performance blocker

The original before/after billing jobs spent prolonged time in
`historical.metadata._first_unique_match`. A separate read-only one-bill
profile timed out at 35 seconds in that matcher. It collected and deduplicated
every matching revision line before returning only the first value; subsequent
filing-tail scans cannot change that answer.

The helper now uses the first match directly. No regex, source text, date rule
or numerical selection rule changes. A read-only prototype completed the same
one-bill workload in 20.119 seconds; this censored, contended diagnostic is not
an isolated speedup estimate. Compatibility cases compare against the previous
collect/deduplicate contract. A bounded child-process regression includes both
headers followed by 100,000 blank lines, preventing a future regression from
hanging the test suite. All 17 metadata tests pass.

The two original slow billing jobs were stopped and their logs preserved as
`billing_unoptimized_before_stopped.txt` and
`billing_unoptimized_after_stopped.txt`. The comparison reruns apply the same
first-match optimization to both D4 and D5 code trees. This is explicitly a
**D4 + identical metadata optimization** baseline, not an unmodified D4 timing
comparison. `billing_baseline_metadata_patch.json` records the baseline exception
and module hashes. The optimization's result equivalence is tested separately;
the billing comparison isolates the remaining D5 changes.

The next profile reached repeated historical-document lookups. Normal
repository connections initialize schema/migrations on each lookup. The
benchmark now offers `--read-only`, retaining the production-path refusal while
reusing one query-only connection to an already-migrated scratch snapshot.
This changes benchmark connection management, not billing calculations, and is
not a measurement of production connection performance. Both comparison sides
use the same mode and snapshot. No new persistent cache or catalog was added.

**All 12 bills match exactly across every recorded per-bill field and the full
summary:** 11 `good_match`, 1 `needs_review`, maximum absolute total difference
from the actual bills **$0.45**, unchanged. Neither run changes official rates.
The normal-connection reruns were stopped after the read-only comparisons
completed; their partial logs remain diagnostic evidence.

Evidence: `billing_readonly_before.json`, `billing_readonly_after.json`,
`billing_comparison.json`, and `billing_readonly_progress_profile.txt`.

Targeted source, queue, archived-page, LLM, overnight and metadata tests:
**161 passed** in 24.86 seconds. New benchmark/test files pass Ruff. Final line wrapping in
modified Python files preserves their AST; unrelated legacy style debt remains.

The first full run reported 1,945 passed and three failures. One expectation
about colored revision text was resolved in the detector. Two overnight tests
used nonexistent source fixtures; they now exercise actual hash-bound PDFs.
Their guard was not stubbed. Before the ancillary metadata optimization, the
full suite passed: **1,954 passed, 92 skipped, 4 xfailed**, 135 warnings, in
**675.94 seconds**. The refreshed full suite passes: **1,969 passed, 92 skipped,
4 xfailed**, 135 warnings, in **808.22 seconds**. All 12 before/after bill
records and their summaries are identical in the read-only comparison.
A separate planning CLI probe preserved its scratch database bytes,
retained the proposed $175 research value, and made zero accepted inserts.
The billing-harness safety probe refuses a production-path alias before the
report, rejects an attempted write in read-only mode, and preserves its scratch
database bytes. Final production hash and mtime still match the post-D4-incident
baseline (`production_hash_final.json`).

GitNexus impact analyses preceded existing-symbol edits. Page evaluation and
validation helpers were HIGH risk; the page summary was CRITICAL (six direct
callers / six process entry points). Risks were reported before editing. Writer
and queue changes were LOW risk; the dataclass was MEDIUM. Test definitions with
no indexed callers were UNKNOWN, not assumed unused. New symbols were checked
again after a full graph rebuild. Renames used GitNexus; its unavailable Windows
`rg` subprocess degraded secondary text search, corroborated with local `rg`.
The metadata helper was separately **CRITICAL**, with one direct caller and
eight affected billing/lineage/audit flows; this was reported before editing.

The final full graph has 20,489 nodes, 39,871 edges, 911 clusters and 975 indexed
flows. Structured staged change detection identifies 86 indexed symbols and
27 affected flows, with **CRITICAL** aggregate risk and no tool error or partial
result. The mapped flows match the intended validation/review/binding and
metadata-dependent billing/history scope. A separate AST comparison confirms
the intended existing functions/classes and test fixtures; numerical rate
profiles are unchanged.

The index warns that its global flow budget omits some entry points; absent
flows do not prove absence of callers. Direct source/caller checks supplement
the graph, including the LLM and overnight entry points. Final receipts:
`gitnexus_detect_final.json`, `gitnexus_final_with_metadata_analyze.txt` and
`ast_scope_review.json`. No database, model cache or scratch tree is staged.
No push or merge is authorized.

## Remaining work

Independently review prioritized source findings before any production repair.
Validate clean counterparts against approval evidence and compare complete rate
sets, applicability, dates and actual billing use. D6 extends local-first clue
resolution using the existing ledger; D7 improves mixed-filing boundaries; D9
investigates unlinked rows, unavailable bindings and historical coverage. No
production remediation is implied by this report.
