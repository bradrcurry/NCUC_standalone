# Phase 2E D2: Portal Reliability, Order Discovery, Redline Safety, Reviewed Promotion

**Date:** 2026-10-28
**Branch:** `phase2e/d2-portal-orders-promotion`, cut from `phase2e/d1-regulatory-clue-ledger` @ `8486ac4`.
**Basis:** D1 report §13–§14, M2 §7.2.

## 0. Baseline facts (measured before any change)

| Fact | Value |
|---|---|
| D1 branch commits over M1.1 (`27f3812`) | 7 (`c652f0a`..`8486ac4`) |
| Production SHA-256 | `fe563a78…cd08`, mtime 2026-10-26 12:36:11 (`reports/phase2e_d2/production_sha_before.txt`) |
| GitNexus `DocumentParamSearcher.search` | **CRITICAL**: 8 direct callers, 7 flows, 3 modules; 150 untyped call sites; index 20 commits stale |
| Captured portal HTML | `data/doc_param_form.html` (filing-type list `disabled` until the checkbox auto-postback), `data/doc_param_results.html` ("Items Count: 211", pager `<span>1</span>` + links 2–10 + `...`) |
| Why 1b72e8de was not flagged | Redline marks are red text plus red **filled bars** (no `char_flags` strike, no annotations). The detector ran only on hd 2275's bound slice, p.1 (the index page). No fingerprint covers pp.100–105, and the local resolver looks fingerprints up by page overlap. 7f394a37 (Leaf 660/661) has the same mechanism, in maroon `#b5082e`. |
| Live portal access | `session.create_authenticated_context` logs in with the user's NCID credentials. No live request is made in D2 without explicit authorization. |

## 1. Work items

| # | Item | Approach | Reuses |
|---|---|---|---|
| W1 | Searcher reliability | Add `DocumentParamSearcher.search_with_outcome()`, which returns results plus stop reason, pages reached, reported item count, grid signatures and filter verification. `search()` keeps its signature and return type and delegates to it. Pure HTML-state functions (block, pager, items count, grid identity, empty-state, filter-list enabled) are tested on fixtures derived from the captured pages. | existing parser, pager logic |
| W2 | Ledger pagination outcomes | Provenance schema v4 adds `stop_reason`, `pages_total`, `items_reported` and `filter_verified` to `search_attempts`. `Attempt.finish_outcome()` maps a search outcome to `hits` / `no_results` / `incomplete` / `blocked` / `error`. | `search_ledger` |
| W3 | Docket-scoped order discovery | Build order targets from `missing_regulatory_order` gaps and blank or observed order-date clues. Search local `ncuc_discovery_records` first, then `DocumentParamSearcher` with `filing_types=["ORDER"]`, the docket, and the date window (channel `portal_orders`). Results go to `remote_document_candidates` (`kind='order'`) with an explicit lifecycle. | searcher, ledger, discovery records |
| W4 | Network gap resolution | Network search only for gaps whose local search completed against the current archive state and found nothing. Plan the queries, deduplicate by `params_hash`, put explicit gaps first, and bound the run. Results are stored as candidates with provenance. Download is optional and deduplicated by URL and SHA-256. | `missing_clean_doc_search` query specs, ledger |
| W5 | Redline page evidence | `redline_detector.page_redline_evidence()` reads coloured text, strike vs underline from **bar geometry** relative to the span bbox, and glued revision strings. The local resolver evaluates the candidate's **own pages**. A redline page cannot be `full`, and a clue whose text is struck does not count as an identity match. | `detect_redline` (unchanged), `regulatory_clues` |
| W6 | Reviewed promotion | Provenance tables `promotion_proposals` / `promotion_snapshots`. Flow: `propose` (automatic checks) → `approve` (named reviewer) → `apply` (snapshot first, one transaction; staged rebind keeps charges) → `rollback`. Proposal kinds are `rebind_page_range` and `mark_redline_duplicate`. The legacy `lineage rebind-historical-page-range` now reports the charges it would delete and refuses unless `--allow-charge-deletion` is given. | `provenance promote`, `extract-candidate`, audit triggers |
| W7 | Effective-date reconciliation | `effective_date_assessments`: stored vs observed (own sheet, non-redline) vs inferred; status `agree` / `conflict` / `ambiguous` / `stored_only` / `observed_only` / `reviewed`. Gap windows use the union of stored and observed dates on conflict, so a wrong stored date cannot exclude a candidate. Nothing in `tariff_versions` is written. | clue ledger |

## 2. Acceptance criteria

| # | Criterion | Target |
|---|---|---|
| S1 | Fixture coverage for search states: success (multi-page), empty, incomplete (pagination error, stale grid, cap), blocked (initial and mid-pagination), failed (form error / filter not applied) | Each state has a deterministic test and a distinct `stop_reason` |
| S2 | Pagination termination recorded | Every `search_with_outcome` returns `stop_reason` and `pages_reached`, plus `items_reported` when the page prints it. The ledger stores them. |
| S3 | Completed identical searches not repeated; failed / blocked / incomplete retryable | Tested at the ledger level and in the order and network planners |
| S4 | Backward compatibility | `search()` signature and return type unchanged. Existing `test_document_param_search.py`, `test_missing_clean_doc_search.py` and the caller tests pass. A filtered search whose filter cannot be applied raises (as Cloudflare does) instead of silently running unfiltered. |
| D1 | Candidates retain provenance | 100% of remote candidates carry channel, attempt id, query hash, portal document id / URL and retrieval time. Downloaded ones also carry SHA-256 and path. |
| D2 | Local-first | A gap gets a network query only if a completed `local` attempt exists for the current archive state. Tested. |
| D3 | No automatic authority | No code path sets an order `linked` or a candidate `verified` without a named reviewer. A docket match alone yields `candidate_discovered`. |
| R1 | D1 redline failures | hd 2275 pages 100–105 (1b72e8de), the Leaf 660 page run (7f394a37 pp.221–225) and the Leaf 661 page (p.226) are flagged redline. The MROP-16A struck match is no longer an identity match. Regression tests use fixture pages cut from the source PDFs. |
| R2 | Clean controls | 7b9171ff pp.96–101 (clean First Revised Leaf 600) are **not** flagged |
| R3 | Struck values never promoted | Promotion refuses a version whose span has redline page evidence, unless a reviewer records a redline-specific override (not offered in D2) |
| P1 | Production unchanged | SHA-256 identical before and after |
| P2 | No charge destroyed during rehearsal | The hd 7265 staged rebind keeps legacy charges until the candidate is validated and promoted. Every replaced row is snapshotted and restorable; rollback restores the byte-identical row set. |
| P3 | Explicit approval | `apply` refuses unless the proposal is `approved` by a named reviewer, and its checks still pass at apply time |
| P4 | hd 7265 / hd 2275 | 7265: rebind proposal passes its checks; after approval, apply, extraction and promotion: 125 charges and 14/14 TOTALs. 2275: rebind proposal **rejected** by the redline check; a `mark_redline_duplicate` proposal → hd 7265 passes. |
| E1 | Date reconciliation | Every version with observed effective-date clues gets an assessment. Version 6548 is reported `conflict` (stored 2024-01-24 vs printed 2025-01-01). Its Fourth Revised gap window then includes 2024-12-01. |
| T1 | Tests | New tests pass. Full suite: no new failures vs the D1 baseline (1,770 passed). |
| T2 | 26-document benchmark | All 6 configurations IDENTICAL to D1 (no extraction code changes), or every difference explained |
