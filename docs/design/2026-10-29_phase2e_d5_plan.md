# D5 — native redline evidence and historical source integrity

Branch: `codex/d5-redline-historical-integrity`, based on D4 commit `90be8be`.
The D4 production preflight incident remains recorded; it is not retroactively
treated as a passed preservation gate. No production repair is authorized.

## Reproduced baseline

A fresh `mode=ro`, `query_only` audit of current production and local PDFs found:

- 411 versions with linked charges; 65 redline-flagged versions.
- 45 strong-evidence versions carrying 1,113 charges, reproducing D3 exactly.
- 5,082 charges join an existing version; 5,198 charge rows exist in total.
  The remaining 116 unlinked rows are a D9 coverage/integrity investigation,
  not grounds for automatic deletion or correction.
- 27 charged versions use utility-web documents outside the historical-span
  audit. One historical binding points to an unavailable pytest temporary PDF.
  Do not mislabel all 28 as missing archived historical sources.
- Baseline source inspection took 63.094 s, opening source PDFs in bounded
  groups and preserving source hashes. All labels are automated, not human
  verification. Evidence: `reports/phase2e_d5/baseline_exposure.json` and
  `baseline_source_coverage.json`.

## Existing capabilities and gaps

- `detect_redline` already reads annotations, colors and **correctly** uses
  `char_flags` bit 0 for strikeout. Its module docstring still describes font
  bit 3 incorrectly; do not mistake that stale prose for the implementation.
- Page evidence already reads strikeout annotations, character strikes,
  chromatic vector bars and glued revision text. It preserves some strike
  geometry internally, but serialized evidence omits most geometry/method
  detail and supplies only four legacy verdicts.
- Underline annotations and character-underlines need contextual treatment.
  Black vector markup and individual path segments need evaluation against
  native controls before adding duplicate logic.
- Insertion corroboration currently counts color anywhere on a page, which
  can combine a colored logo with an unrelated underlined heading. Require
  evidence attached to the same affected text.
- Legacy annotation scoring treats highlight/underline alone as strong redline
  evidence. Benchmark decorative annotations and headings explicitly.
- `proposed_tariff_promoter.apply_promotion` directly inserts accepted families,
  approved versions and charges. Its CLI confirmation and idempotency checks
  do not establish source integrity or regulatory approval. Accepted-lineage
  writes must use the existing reviewed binding/candidate/promotion workflow;
  isolated proposal data and planning must remain usable.

## Acceptance gates

1. Audit supported authoritative-write entry points and their source guards,
   including the proposed shortcut, LLM promotion, legacy replacement and rider
   pathways. Document unsupported low-level/manual writers explicitly.
2. Extend the current native detector, preserving its public compatibility
   verdicts. Serialize page, bbox, exact text where available, method, strength
   and inferred classification. Support confirmed material redline, likely,
   ambiguous, no detected evidence, and insufficient evidence without equating
   the last two to regulatory approval.
3. Distinguish observed features from classification. No agent assessment or
   classifier agreement becomes independently human-verified ground truth.
4. Use a reproducible benchmark from actual archived pages, including clean
   tariffs, redlines, mixed filings, decoration/table/heading false positives,
   and scans/flattened pages. Keep synthetic structural controls separate.
   Report precision/recall against explicitly attributed source-review labels,
   page attribution, time, rendering and OCR requirements; no universal claim.
5. Recompute the 65-version exposure set, prioritizing the 45 strong cases.
   Produce a source-grounded remediation/review queue using existing review
   infrastructure and candidate records. Compare clean counterparts, dates,
   stored values and billing exposure where evidence supports it. No production
   charge/date/status edit, binding, promotion or deletion.
6. Prevent unattended authoritative writes from bypassing source-integrity
   checks. Preserve valid proposed-rate research and existing explicit reviewed
   promotion/rollback protections. Do not add a competing approval system.
7. Targeted tests during development; extraction benchmarks for extraction
   changes, billing benchmarks for billing-relevant promotion changes; full
   suite and fresh GitNexus change review before local milestone commits.
8. Final report records measured improvements, remaining limits, label
   provenance, production hashes, review requirements and next milestone.

## Impact analysis before edits

Fresh GitNexus index at the D4 commit:

- `evaluate_redline_page`: **HIGH**, one direct reader caller, three affected
  process entry points including source validation/binding/review flows.
- `detect_redline`: **LOW**, three direct callers in the indexed analytics area.
- Proposed `apply_promotion`: **LOW**, one direct CLI caller/process.

These risks were reported before editing. Inspect additional symbols before
changing them. Full rebuilds are required when incremental index output drops
symbols or misattributes unrelated flows, as observed during D4.

## Current step

Implementation complete. The source-reviewed corpus has 29 pages from 11
archived PDFs: 11 true positives, 0 false positives, 5 abstentions; no independent
human labels. The shared source audit reports 57 redline-exposed versions,
47 strong cases / 1,120 charges, 79 insufficient-source versions and one missing
historical source. The scratch queue preserves prior decisions and now has 248
open items, with no human-review labels. Production still matches its post-D4
incident hash; that incident remains an overall preservation failure.

Validation: 161 targeted tests and a refreshed full suite of 1,969 passed,
92 skipped and 4 xfailed in 808.22 seconds. All 12 before/after bill records and
their summaries match exactly. A validation blocker led to an equivalent
first-match metadata optimization (CRITICAL impact reported before editing)
and an explicit read-only batch mode in the benchmark. Both comparison trees
use the same metadata optimization; the baseline exception is recorded.
Final staged GitNexus review reports 86 indexed symbols / 27 expected flows,
CRITICAL aggregate risk, with no tool error or partial result. The AST diff and
direct writer-call audit corroborate the scope; index flow-budget limitations
are recorded. Ready for local commit. Full report:
`docs/reports/2026-10-29_phase2e_d5_redline_historical_integrity.md`.
