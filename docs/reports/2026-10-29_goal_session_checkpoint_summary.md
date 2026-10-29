# NCUC document intelligence — goal-session checkpoint and handoff

The user requested a natural stopping point with the work PR'd, merged and a
clean main checkout. The selected checkpoint is **D7A segmentation and routing**.
D4–D6 engineering deliveries and D7A form the integrated delivery; D7B and
D8–D10 remain unfinished. The original D4–D10 goal must not be marked complete.
Publication/merge commit identities are provided in the PR description and final chat receipt.

## Repository and publication scope

The session began at `de376bb` on `phase2e/d3-live-discovery-binding` in
`C:/Python/Duke/Standalone`. The initial tracked tree was clean; existing
untracked scripts/evidence were preserved. D4, D5, D6 and D7 used dedicated
branches. No source history was reset or rewritten.

The current engineering chain includes 66 prerequisite commits through D3 that
were not yet on main, plus this session's milestone commits. They are necessary
dependencies, not work newly authored during this session. The integrated PR
preserves their history and authorship. At the user's additional request, RAG
PR #25 was reviewed separately. It is
held despite a clean text merge and 76 passing compatibility tests because its
own evaluation reports a citation-grounding regression. Its commit is preserved;
there is no separate retrieval/debug hunk to cherry-pick. See the linked review.
No databases or original archive directories are being published.

A clean main worktree will be used after merge, preserving existing loose files
in the original checkout. Verify the PR merge receipt, remote-main ancestry and
working-tree status rather than assuming that a local commit means publication.

## Delivered work

| Delivery | Local commit | Result |
| --- | --- | --- |
| D4 complete docket inventory | `90be8be` | Verified multipage traversal, recovery, restricted-document handling, ledger outcomes and safe read-only eligibility preflight. Engineering validated; original production-preservation gate failed. |
| D5 redline/source integrity | `7dc5caa` | Native page/geometry evidence, exposure audit and review queue, stronger source gates, blocked unsafe promotion bypasses, preserved proposed research and billing checks. |
| D6 evidence-driven discovery | `2555e98` | Evidence-sensitive local-search caching, exact multi-docket source references, additive document relationships, durable hash-addressed retrieval and review preservation. |
| D7A segmentation/routing | See checkpoint PR | Source-reviewed reference, formal tariff ownership, non-tariff boundaries, cache metadata preservation, corrected large-PDF triage and both non-tariff import guards. |

### D4

The live E-7 Sub 1026 inventory was traversed across all six pages: 568 unique
records, 85 typed orders, of which 83 are public. Eleven records were restricted,
including two orders. Public order coverage increased from 33 on page 1 to 83
across the complete inventory. The September 24 and October 25, 2013 order
listings were found beyond page 1. These are listing facts; no PDF was downloaded.

The browser traversal took 18.73 seconds after loading the first page. An
HTTP comparison received 403 and stopped; no access bypass was adopted.
Recovery, stale/repeated pages, incomplete results, caps, stable IDs and sanitized
checkpoints are covered by tests and six real offline fixtures. D4 full suite:
1,894 passed, 92 skipped, four xfailed.

### D5

Redline evidence retains exact page text, geometry, fonts/colors, annotation
quads, method/version and uncertainty. A 29-page, 11-PDF development benchmark
changed from 11 true positives / one false positive to 11 true positives /
zero false positives, with five abstentions after the change. Labels were
assistant-created source review, not independent human verification or a
held-out recall study. No scanned-positive recall guarantee was established.

The read-only production audit found 57 source-risk-exposed charged versions,
47 strong cases and 1,120 associated charges. These are source-risk findings,
not proof that every associated numerical charge is wrong. Seventy-nine
versions had insufficient native source evidence. A missing source, a comparable
hash mismatch, utility-web exceptions and 116 unlinked charge rows remain
explicit follow-up work. The scratch review queue has 248 open items, including
211 rule-derived and 37 assistant entries; no new human verification was claimed.

Shared validation now checks readable bound pages and stronger own-source
integrity before LLM charge promotion. The old proposed-tariff SQL promotion
bypass refuses actionable writes; planning/research remains available. A
first-match metadata optimization preserves the previous matching semantics;
the billing baseline carries the same optimization, as documented. Full D5
suite: 1,969 passed, 92 skipped, four xfailed. All 12 billing records and their
summary match the qualified before/after comparison exactly.

### D6

Local discovery adds 185 partial candidate records across 70 existing gaps.
Net gaps having candidates increase from 206 to 212. Total candidates increase
from 628 to 813. Three prior full candidates become partial because their own
pages lack sufficient readable evidence; full candidates fall from 87 to 84.
No candidate was bound or promoted.

The revised cache avoids all 370 repeated local searches on unchanged evidence,
with an 11.7-second measured repeat after lazy indexing. Artifact contents,
method versions, catalog changes and file availability invalidate searches.
Incomplete coverage never becomes completed negative evidence. The remaining
network query plan falls from 245 to 241 distinct queries; these are planned
queries, not measured HTTP request counts. D6 made no live request or download.

The source ledger gains 4,316 reference observations with exact page/hash/text
and available geometry. Filing docket identity remains separate from caption
and body references. The additive relationship table expresses proposed
revision, approval/implementation assertions, references and clean-counterpart
hypotheses without a second graph database. Docket agreement does not verify
approval. Newly retrieved PDFs are validated and atomically archived by SHA-256
through existing storage/blob infrastructure; fixture tests exercise deduplication
and mutation failures. Actual live retrieval was unnecessary.

All canonical table fingerprints, prior review states and the 248-item queue are
preserved. Existing 45 machine-verified supersession links and 16 verified/resolved
gap states remain; 675 of 691 active gaps are unresolved. D6's full run passed
1,983 tests, with 92 skips and four xfails; its final focused run passed 78,
including the final added edge cases. The 50-page clue scores and all 12 bill
records/summaries are unchanged.

### D7A

Original source review established 104 formal tariff components / 317 tariff
pages across four actual filings totaling 2,121 pages. Reference labels are
assistant-reviewed, not human-verified or held out, and do not claim exhaustive
fine-grained legal-appendix boundaries. Source anomalies are retained, including
two distinct components printing Leaf 605 and the headerless PPBE appendices.

Native tariff-book component recovery at IoU >= 0.9 improves from 89/103 to
103/103. Cached-artifact recovery improves from 77/103 to 97/103. Both paths
isolate the three-page DSM/EE tariff form. False tariff pages across the four
filings fall from 1,804 to zero on this declared development reference. The
remaining six cached-artifact boundary cases require reconciliation/review;
page recall alone does not prove correct logical ownership.

Triage no longer treats five sampled pages as evidence for every page in a
large PDF. The 598-page appeal and 1,097-page DSM/EE filings stop being routed
to whole-file OCR. Page/span caches preserve flags and boundary evidence, and
both importer entry points reject non-tariff spans before family binding.
No new expensive conversion/model path was introduced. Recorded peak process
working-set/RSS proxy was 299.9 MiB; no GPU conversion ran and VRAM was not measured.
All 12 billing results remain exactly unchanged. Final full validation: **1,998 passed, 92 skipped, four xfailed** in 606.04
seconds. Thirteen new checkpoint cases pass, including both import entry points.
An initial full run's sole failure was a fixture hardcoding the prior span cache
version; it now uses the current version constant while retaining the stale-row
assertion. The final rerun is green. AST review identifies nine production
functions/methods plus that fixture update and the span evidence field; numerical
parser and billing files are unchanged.

## Production-write incident — unresolved acceptance failure

During D4, I ran the documented `run-continuous-loop-nc --dry-run --max-cycles 0`
preflight. It opened production through a migration-capable connection helper
and changed the database file's mtime/hash/schema state. That violated the
user's production-preservation boundary. The path was fixed to use read-only
access, and the incident was documented. No restoration was attempted because
there is no exact pre-command image available in this session.

All 79 shared application tables match a preserved historical D1 reference
row-for-row; the internal sequence differs. That is not an exact before/after
snapshot and does not undo the write. Later milestone hashes and modification
times match the recorded post-incident production state. The original
production-preservation gate remains **FAILED**.

See [incident report](2026-10-29_d4_preflight_safety_incident.md). Source-code
merge does not remediate this database incident or authorize data promotion.

## Remaining work

1. **D7B:** harden opt-in bounded conversion against partial/internal-chunk
   results; adaptive chunks, overlap, original page/table ownership, continuation
   and representation-cache identity. Reconcile six cached boundary cases and
   extend independent/held-out labels. Existing bounded conversion was not
   modernized by D7A.
2. **D8:** inspect isolated Docling environments, configurable layout/model
   identity and TableFormer baseline; measure numerical/resource gates and
   rollback before adoption. No new large model was downloaded in this session.
3. **D9:** recompute gap taxonomy and conduct targeted candidate-only historical
   extraction/remediation, including Leaf 600 and the remaining source/charge
   integrity cases. Preserve numerical, unit, page and TOTAL validation and
   explicit review/promotion/rollback requirements.
4. **D10:** training readiness, honest label provenance, related-document split
   isolation and reproducible export if justified. Assistant fixtures and
   machine-reviewed fields must not become human training truth.

No accepted rate, effective-date or regulatory-status remediation has been
performed. Any production repair/promotion remains a separate reviewed action.
The requested stopping point pauses the larger goal; it does not complete D4–D10.

## Detailed reports

- [D4 retrieval](2026-10-29_phase2e_d4_complete_docket_retrieval.md)
- [D5 redline/source integrity](2026-10-29_phase2e_d5_redline_historical_integrity.md)
- [D6 evidence-driven discovery](2026-10-29_phase2e_d6_evidence_driven_discovery.md)
- [D7A checkpoint](2026-10-29_phase2e_d7a_segmentation_checkpoint.md)
- [RAG PR #25 review and cherry-pick assessment](2026-10-29_pr25_rag_review.md)
- [Roadmap](../plans/ncuc_document_intelligence_roadmap.md)
