# Phase 2E D2: Portal Reliability, Order Discovery, Redline Safety and Reviewed Promotion

**Date:** 2026-10-28
**Branch:** `phase2e/d2-portal-orders-promotion`, cut from `phase2e/d1-regulatory-clue-ledger` @ `8486ac4` (D1's seven commits confirmed). Not pushed or merged.
**Plan and acceptance criteria:** `docs/design/2026-10-28_phase2e_d2_plan.md`, written before implementation.
**Data:** scratch byte copies only.
- `d2.db`: copy of D1's `d1.db` (ledger, reviews, 370 local searches), migrated to provenance v4.
- `d2_promo.db`: fresh copy of production, migrated to v4, for the promotion rehearsal.

Production was never opened for writing; its SHA-256 is unchanged (§10).
**Artifacts:** `reports/phase2e_d2/`, `tests/fixtures/d2_portal/`, `tests/fixtures/d2_redline/`.

Evidence labels:
- **[Measured]**: counted on scratch this session.
- **[Hand-verified]**: read from a rendered page image this session.
- **[Rule]**: machine rule output.
- **[Unverified]**: a candidate nobody has reviewed.
- **[Proposed]**: future work.

---

## 0. Summary

| Acceptance criterion (plan §2) | Result |
|---|---|
| S1: fixtures for success / empty / incomplete / blocked / failed | **Met.** 16 tests on pages derived from the captured 2026-03-18 portal HTML. Every state has its own `stop_reason`. |
| S2: pagination termination recorded | **Met.** `search_with_outcome` returns `stop_reason`, `pages_reached`, `pages_total` and the portal's own `items_reported`. The ledger stores them (schema v4). |
| S3: no repeat of completed searches; failures retryable | **Met.** Tested at ledger, planner and executor level. |
| S4: backward compatibility | **Met.** `search()` signature and return unchanged. The 84 tests of every searcher caller pass. Two deliberate behaviour changes, listed in §2.3. |
| D1–D3: provenance, local-first, no automatic authority | **Met** (tests). **No live network request was made** (§4.4). |
| R1–R3: redline safety | **Met.** All three D1 failures are flagged from their own pages; the clean control is clean. A struck identity no longer matches. Promotion of redline versions is refused on the reviewer path (§5, §6). |
| P1–P4: production, no charge loss, approval, rehearsal | **Met.** hd 7265 rebind kept all 104 charges, then reproduced 125 charges and 14/14 TOTALs. Promotion and full unwind are byte-identical. hd 2275 rebind is blocked; it is recorded as a redline duplicate instead (§6). |
| E1: effective-date reconciliation | **Met.** 578 versions assessed, 35 conflicts. Version 6548 is a conflict (stored 2024-01-24, printed 2025-01-01). The three on-disk Fourth Revised Leaf 600 candidates go from `partial` to `full` (§7). |
| T1 / T2: tests and benchmark | See §8 and §9. |

**Headline findings:**
1. **Why 1b72e8de was never flagged** [Measured]. Its redline marks are coloured filled bars drawn through or under coloured text, with no strikeout flag and no annotation. The detector also ran only on hd 2275's *bound* slice, which is the index page. No stored fingerprint covers pp.100–105, so the D1 resolver had nothing to look up. 7f394a37 (Leaf 660/661) uses the same mechanism in maroon.
2. **45 of 195 local candidates sit on redline pages** [Rule]. 12 were rated `full` in D1. One of them, candidate 554, was hand-verified in D1 as the right identity. Its page image [Hand-verified] is a redline exhibit ("~~May~~April", "~~September~~October"): identity right, but not a clean sheet.
3. **`provenance promote` would have promoted the hd 7265 candidate with a wrong date reading** [Measured]. The run takes its effective date from `historical_documents.effective_start`, which is 2025-09-01. The pages print October 1, 2024. The reviewer path now refuses this (§6.3).
4. **`_find_next_page_target` followed the backward ellipsis on a last page past the first pager window** [Measured on fixtures]. On a 22-page result set the old loop would walk back through earlier pages, appending duplicates, until the 100-page cap. Fixed.
5. **Commission-wide dockets (M-100 …) must not be filtered by company**, and an order search must not take its date window from a tariff's effective date. Either would yield false `no_results` (§3).

## 1. Implementation summary

| Work item | Files | What it does |
|---|---|---|
| W1 searcher | `historical/ncuc/document_param_search.py` | `search_with_outcome()` plus pure page-state functions: `is_blocked_page`, `items_reported`, `pager_current_page`, `grid_signature`, `explicit_empty_result`, `filing_type_list_disabled`. `search()` delegates to it. |
| W2 ledger | `db/discovery_review_schema.py`, `db/provenance_schema.py`, `historical/ncuc/search_ledger.py`, `missing_clean_doc_search.py` | Schema v4: `search_attempts` gains `stop_reason`, `pages_total`, `items_reported`, `filter_verified`. `Attempt.finish_outcome()`. A `PortalSearchError` records its own outcome. |
| W3+W4 discovery | `historical/ncuc/remote_discovery.py` | Read-only planner. Bounded executor that stops when blocked. Retrieval with dedupe. Identity check on the document's own pages. Named review. |
| W5 redline | `parse/redline_detector.py` (new functions only), `local_gap_resolution.py` 1.2.0 | `PageRedlineReader` and `evaluate_redline_page`; `text_struck()` for clue occurrences. The resolver reads candidate pages. |
| W6 promotion | `historical/ncuc/document_promotion.py`, CLI guards in `cli_commands/provenance.py` and `lineage.py` | Propose → approve → apply → rollback, with snapshots. Staged rebind. Redline and date guards on `provenance promote`. Charge-deletion guard on the legacy rebind. |
| W7 dates | `historical/ncuc/effective_date_reconciliation.py` | `effective_date_assessments`. The resolver widens gap windows on conflict. |
| CLI (12 new) | `provenance` | `reconcile-effective-dates`, `plan-network-searches`, `run-network-searches`, `retrieve-remote-candidates`, `review-remote-candidate`, `propose-rebind`, `propose-redline-duplicate`, `approve-proposal`, `reject-proposal`, `apply-proposal`, `rollback-proposal`, `proposals`. Snapshot 359 → 371, updated deliberately. |
| Tests | `test_d2_portal_search.py` (16), `test_d2_redline_and_dates.py` (16), `test_d2_remote_discovery.py` (8), `test_d2_promotion.py` (6) | 46 new tests. |

Commits (7, logical):
- `26593bf` schema v4 + ledger;
- `4d21275` searcher;
- `9ddd155` redline evidence;
- `6645021` date reconciliation + resolver;
- `369a369` discovery;
- `b89159b` promotion;
- `dcdfb69` promote date guard + rehearsal artifacts.

The report commit follows.

**Reused, not duplicated:**
- `DocumentParamSearcher` with the ORDER filing-type GUID, for docket-scoped Orders search. `portal_scraper.scrape_recent_orders` only lists recent orders, not by docket.
- `NcucDiscoveryService.search_public_site` (zoom).
- `session.download_view_file`.
- D1's `SearchLedger`, `canonical_params`, `clue_ledger.file_identity`, `regulatory_clues.extract_page_clues`, `clue_lineage.load_version_evidence` / `family_clues`, `local_gap_resolution.archive_state`.
- `provenance extract-candidate`, `promote` and `rollback`, unchanged.
- The provenance audit triggers (`metadata_revisions`, `tariff_charge_revisions`).

## 2. Search reliability (W1, W2)

### 2.1 What the captured pages show [Measured]

- `data/doc_param_form.html`: `filingTypesList` is `disabled="disabled"`, and the checkbox `onclick` is `__doPostBack(...)`. The list cannot take a selection until that postback returns. This confirms the M2 §7.2 suspicion: the old `check` → `sleep(0.5)` → `select_option` could run an **unfiltered** search silently.
- `data/doc_param_results.html`: "Items Count: 211"; 10 result tables per page; pager row `<span>1</span>` then links 2–10 and `...`.

The pages were copied to `tests/fixtures/d2_portal/` with hidden ASP.NET state blanked. Later pages, the empty page and the blocked page are derived in the test file, visibly.

### 2.2 Stop reasons

| Class | `stop_reason` | Ledger `outcome` | Negative evidence? |
|---|---|---|---|
| complete | `last_page`, `single_page`, `empty_result` (portal says "Items Count: 0") | `hits` / `no_results` | `no_results` only |
| incomplete | `max_results_cap`, `page_cap`, `pagination_error`, `stale_grid` (postback did not show the next page with new rows), `item_count_mismatch` (fewer rows than "Items Count") | `incomplete` | no; retryable |
| blocked | `blocked_initial`, `blocked_pagination` | `blocked` | no; retryable; the executor stops the run |
| error | `form_unavailable`, `filter_not_applied`, `submit_error`, `unrecognized_page` (no grid **and** no empty-result statement) | `error` | no; retryable |

"Genuinely empty" now requires the portal to say so. A page with no rows and no count is `unrecognized_page`, not `no_results`.

### 2.3 Behaviour changes (deliberate; each recorded on the outcome)

1. **A filtered search whose filter cannot be verified is not submitted.** `search()` raises `PortalSearchError` (a `RuntimeError`, the same type as the existing Cloudflare raise). Before, it ran unfiltered. All callers already wrap `search()` in try/except.
2. **Pagination waits for the expected page with new rows**, instead of a fixed `sleep(2)`, and rows are de-duplicated across pages. The backward-ellipsis bug is fixed. Otherwise `search()` returns what it returned before, including rows collected before a pagination failure.

The filter is now applied **before** the text fields (the postback re-renders the form), and every field is read back.

### 2.4 Impact analysis [GitNexus]

| Symbol | Risk | Callers / flows | Handling |
|---|---|---|---|
| `DocumentParamSearcher.search` | **CRITICAL** | 8 direct, 7 flows (missing-doc workflow, harvester, `ncuc portal-search`, `search doc-param`, `search run`, 2 ingestion scripts); 150 untyped call sites | Signature/return unchanged; delegates. 84 caller tests pass (`test_cli_ncuc_portal_workflow`, `test_ncuc_portal_harvester`, `test_missing_*`, D1, D2). |
| `_find_next_page_target` | **CRITICAL** (via `search`) | 1 direct | 2 existing + 2 new tests. *Impact was run after this edit, not before; reported here.* |
| `_run_structured_searches` | LOW | 1 flow | Uses `last_outcome` when the searcher has one; stub searchers fall back to the old `finish()`. |
| `ensure_provenance_schema` | LOW | `migrate_cmd` | Additive (tables + 4 added columns). |

## 3. Docket-scoped regulatory order discovery (W3)

### 3.1 Targets and plan [Measured on `d2.db`, `reports/phase2e_d2/network_plan.json`]

| Source | Targets |
|---|---:|
| `missing_regulatory_order` gaps (26 explicit, 87 inferred), all `searched_not_found` locally | 113 |
| Blank order-date clues (`Order dated ____`) with a docket on the same page | 102 |
| **Distinct `portal_orders` queries** (88 dockets) | **103** |

Rules:
- `filing_types=["ORDER"]` and the docket.
- Company filter only for E-2 / E-7; commission-wide dockets (M-100 …) are not filed by Duke and would be emptied by it.
- A filed-date window **only around a printed order date** (±7 days; 6 targets). Everything else is docket-only.

An earlier draft derived windows from the tariff's effective date. It was removed: a 2024 sheet can cite a 2013 order, and a guessed window would turn a real order into a completed `no_results`.

The measured blank order-date clue count is 220, not D1's "464": that figure counted blank clues of every kind.

### 3.2 Lifecycle (distinct states)

| State | Set by | Meaning |
|---|---|---|
| `listed` | executor | **Candidate discovered.** A search returned it. Score per criterion: `docket_match`, `date_in_window`, `order_wording`. |
| `needs_review` | `retrieve_and_check` | **Identity checked by rule.** File downloaded (SHA-256), and its own pages read. Checks: `docket_printed` (case-insensitive caption pattern, `DOCKET NO. E-2, SUB 1300`), `order_wording_printed`, and dates. |
| `linked` | `review_candidate` only, named reviewer | **Order linked to a tariff version.** Requires a retrieved file and a named version. |
| `rejected` | `review_candidate` only | Reviewed and not applicable. |

A docket match alone never moves a candidate past `listed` (tested). A gap moves only to `candidate_found`, never `verified` or `resolved`.

## 4. Network gap resolution (W4)

### 4.1 Local first [Measured]

A gap is planned only if a `local` attempt for that gap completed against the **current** archive state and resolver version. All 249 `searched_not_found` gaps qualify after the 1.2.0 rerun (D1 had 248; the rerun moved one). A gap marked `searched_not_found` without such an attempt is skipped (tested).

### 4.2 Plan

| Channel | Distinct queries | From |
|---|---:|---|
| `portal_orders` | 103 | §3 |
| `portal_params` | 53 | 29 explicit predecessor + 5 explicit clean-tariff gaps: docket candidates (≤3) and the reconciled window (−120 / +31 days) |
| `zoom` | 134 | printed expected revision + company; 102 are inferred intermediate revisions |
| **total** | **290** | 409 targets. Explicit gaps first, then by gap type. |

### 4.3 Execution properties (tested with fakes)

- Bounded (`--max-queries`).
- Resumable: completed hashes are skipped; `blocked` / `error` / `incomplete` are retried.
- Stops at the first blocked search, with no retry loop.
- Every listing keeps attempt id, `params_hash`, portal document id, detail and ViewFile URLs, the parsed row, and its score.
- Retrieval dedupes by URL and by SHA-256 against `historical_documents` and `ncuc_discovery_records` (`download_avoided_same_url`, `already_in_archive`).

### 4.4 Not executed

`run-network-searches` and `retrieve-remote-candidates` log in to the NCUC portal with the account's NCID credentials (`session._do_login`). They refuse to run without `--live`. **No live request was made in D2**: the brief rules out uncontrolled portal requests, and logging in to a production government site needs your authorization. Results therefore stop at the plan. The `--live` dry run prints exactly which queries would run.

Suggested first supervised run: `--channel portal_orders --explicit-only --max-queries 10`, on a scratch copy.

## 5. Redline detection (W5)

### 5.1 Page evidence [Measured / Hand-verified]

For each span, `evaluate_redline_page` finds:
- coloured text;
- coloured hairline bars (height < 2.6 pt);
- bar position relative to the span: centre at 30–78% of span height = **struck**; 78–120% = **inserted** (underline);
- strikeout flags, StrikeOut annotations, and glued revision text.

Verdict: `redline` / `suspect` / `clean` / `no_text_layer`. `text_struck()` checks **every case-exact printed occurrence** of a clue. A struck suffix ("MROP-~~16A~~", ≥25% of the occurrence) counts as struck.

| Page (fixture) | D1 problem | D2 verdict |
|---|---|---|
| 1b72e8de p.100 (hd 2275 target) | not flagged; 11 struck RAL-2 rows extracted | `redline`: 11 struck / 5 inserted spans; `RAL-2 Rider` and `-0.009` struck |
| 7b9171ff p.96 (hd 7265 target, control) | — | `clean`: 0 coloured / 0 struck |
| 7f394a37 p.221 (D1 #10, Leaf 660) | `full` | `redline` |
| 7f394a37 p.226 (D1 #19, Leaf 661) | struck "RIDER MROP-16A" matched as identity | `redline`; `RIDER MROP-16A` struck (title suffix + footer); `Superseding NC Rider MROP-16A` **not** struck (inserted) |

Each verdict was checked against the rendered page (p.226 image read this session). `detect_redline` itself is unchanged; its stored per-slice verdicts are unaffected.

### 5.2 Resolver 1.2.0 on `d2.db` [Measured; `resolve_local_v12_summary.json`]

| | D1 (1.1.0) | D2 (1.2.0) |
|---|---:|---:|
| Candidates full / partial | 92 / 103 | 87 / 108 |
| full → partial (page redline) | — | 12 |
| partial → full (window widened, §7) | — | 7 |
| Candidates on redline pages (any strength) | not measured | 45 |
| `identity_struck` (kept for audit; does not advance the gap) | — | 6 (#19 = cand 525; 482, 485, 499, 508, 517) |
| D1 spot-check #10 (cand 523) | full | **partial** (page_redline) |

Notes:
- Struck-identity candidates are all on the 7f394a37 Leaf 6xx redlines. The 482 and 508 crops were viewed and are consistent [Hand-verified crops]; the other 3 are [Rule].
- **Candidate 554** (DEC Leaf 226, D1 `verified`) is a redline exhibit [Hand-verified image]. Its review state is kept; its strength is now `partial`.
- `verification_required` is now re-evaluated on reruns; only `verified` / `resolved` are locked.

## 6. Reviewed promotion workflow and rehearsal (W6)

### 6.1 Design

**Proposal kinds:**
- `rebind_page_range`;
- `mark_redline_duplicate`, which writes only `document_assertions`.

**Checks**, stored per proposal and **re-run at apply**:

| Check | Blocking? |
|---|---|
| File present | yes |
| Pages in file | yes |
| Family leaf printed on the first page | yes |
| No other leaf in the run | yes |
| Printed utility = family utility | yes |
| A single revision identity | yes |
| Pages not redline | yes |
| A single tariff version | yes |
| No redline-duplicate assertion | yes |
| File hash vs stored hash | no; reported |
| Effective date printed vs stored | no; reported |
| Overlapping bindings | no; reported |
| Conflicting lineage links | no; reported |
| Charges the legacy rebind would delete | no; reported |

**Approval:** a named reviewer other than the proposer, with a note.

**Apply:** one transaction under `write_context`, so the audit triggers attribute every change:
1. snapshot `before`;
2. change;
3. snapshot `after` (document, versions, charges, assertions).

A rebind changes only the page bounds and clears cached text, so extraction reads the new pages. **Charges are kept** until a candidate over the new pages is promoted through the existing audited `provenance promote`.

**Rollback:** restores the snapshot. It refuses while a later charge promotion is in place (undo that first with `provenance rollback`).

**Guards:**
- `lineage rebind-historical-page-range` now prints how many charges it deletes, and refuses unless `--allow-charge-deletion`.
- `provenance promote` refuses a run whose version's pages carry redline markup or a redline-duplicate assertion (`--acknowledge-redline`).
- `provenance promote` also refuses a run whose effective-date reading is not printed on those pages (`--acknowledge-date-conflict`).

`pv.promote` itself (**CRITICAL**: callers in `ncuc_loader` and `bulk_extractor`) is **not** changed (§11).

### 6.2 Rehearsal on `d2_promo.db` [Measured; `reports/phase2e_d2/rehearsal/`]

Approvals were made by rehearsal actors (`d2-rehearsal-*`) on scratch. They are **not** human reviews.

| Step | hd 7265 (version 6908) | hd 2275 (version 6056) |
|---|---|---|
| Before | bound p.1, 104 charges | bound p.1, 136 charges |
| Propose rebind | 96–101 → **proposed**, all blocking checks pass | 100–105 → **blocked**: `pages_not_redline`, `single_revision_identity` ("First RevisedOriginal"), `first_page_prints_family_leaf` |
| Apply | pages 96–101; **104 charges kept, 0 deleted**; 2 `metadata_revisions` by trigger | — |
| Candidate (hybrid) | **125 charges, 14/14 TOTALs**, validation passed (= D1 / M1.1) | — |
| `provenance promote` | **refused**: run reads 2025-09-01 (hd metadata), pages print 2024-10-01 | — |
| Redline duplicate | — | `mark_redline_duplicate` → 7265: proposed, applied; 0 legacy rows changed |
| Promote a 2275 run | — | **refused** (`redline_duplicate_assertion`) |
| Rebind 2275 again | — | blocked, now also `no_redline_duplicate_assertion` |
| Promote 7265 with `--acknowledge-date-conflict` (mechanism test only) | 104 deleted / 125 inserted; 229 `tariff_charge_revisions` | — |
| Rollback proposal while charges promoted | **refused** (undo charge promotion first) | — |
| `provenance rollback`, then `rollback-proposal` 3 and 1 | charges and `historical_documents` row **identical to before** (SHA-256 of all 104 charge rows equal; `raw_text_path` restored) | 136 charges identical; assertion retracted |

### 6.3 Production actions this supports (not taken; need your approval)

1. **hd 7265:**
   - correct `historical_documents.effective_start` (2025-09-01 → 2024-10-01, printed on pp.96–101) as a reviewed metadata correction;
   - then propose / approve / apply the rebind to 96–101;
   - then extract the candidate, review, and promote.
2. **hd 2275:** `mark_redline_duplicate` → hd 7265. Do not re-extract it as a clean sheet.

## 7. Evidence consistency: effective dates (W7) [Measured]

578 versions assessed (`effective_date_assessments.json`):

| Status | Count |
|---|---:|
| agree | 269 |
| stored_only | 206 |
| none | 49 |
| conflict | 35 |
| observed_only | 12 |
| ambiguous | 7 |

There are 2 inferred-bound violations (verified-link order vs observed dates).

Conflicts are review leads, not corrections; no `tariff_versions` row is written. Examples:
- 6548 Leaf 600: 2024-01-24 vs 2025-01-01;
- 6059 Leaf 601: 2024-01-16 vs 2025-01-01;
- 5990 DEC PS: stored 2026-10-13 vs printed 2009-10-13.

Effect on gap evaluation: for D1's DEP Fourth Revised Leaf 600 gap (anchor 6548), the window end moves 2024-01-24 → 2025-01-01, with the basis recorded. The three on-disk Fourth Revised sheets (effective 2024-12-01), which D1 rated `partial` / `effective_in_window=False`, are now `full`. Two of them were hand-verified in D1. Seven candidates moved partial → full this way.

## 8. Benchmark (26 documents, 6 configurations) vs D1

`bench/run_extraction.py` on `d2.db` (`reports/phase2e_d2/bench/`). Diffed against `reports/phase2e_d1/bench/` with `bench/phase2d/diff_runs.py --evidence`:

| Configuration | Verdict vs D1 (= M1.1) |
|---|---|
| committed cache: legacy / docling / native / hybrid | **IDENTICAL** / **IDENTICAL** / **IDENTICAL** / **IDENTICAL** |
| production artifacts (`--docling-source db`): hybrid / docling | **IDENTICAL** / **IDENTICAL** |

This is expected: D2 changes no extraction, segmentation or billing code. The frozen billing baseline was not re-run for the same reason. Production is untouched, and on scratch only the rehearsal versions' charges changed, and were restored.

## 9. Tests

All runs use `.venv312` (Python 3.12), the project environment. Under the system Python 3.14, a site-packages `tests` package shadows `tests/`, so D1's test imports fail there.

| Run | Result |
|---|---|
| New D2 tests (`test_d2_*`) | 46 tests, all pass |
| Searcher-caller tests (`test_cli_ncuc_portal_workflow`, `test_ncuc_portal_harvester`, `test_missing_clean_doc_search`, `test_missing_doc_*`, `test_document_param_search`, D1, D2) | 84 passed |
| `test_repository.py` (legacy rebind) | passes |
| `test_cli_command_surface.py` | passes after the deliberate snapshot update (359 → 371) |
| **Full suite** (`reports/phase2e_d2/pytest_full.txt`) | **1,818 passed, 0 failed**, 92 skipped, 4 xfailed (11 min 38 s). D1: 1,770 passed, 0 failed. |

## 10. Production safety

Production `data/db/duke_rates.db` (`reports/phase2e_d2/production_sha_{before,after}.txt`):
- SHA-256 `fe563a78…cd08` before and after;
- mtime 2026-10-26 12:36:11 both times;
- `-wal` 0 bytes.

**Identical.**

Additional facts:
- Every write command added in D2 refuses production unless `--allow-production` is passed.
- The one blocked attempt this session: a manual CLI invocation that would have used the configured database. The tool permission layer stopped it before it ran; nothing executed.
- `gitnexus analyze` rewrote `CLAUDE.md` / `AGENTS.md`. Both were restored with `git checkout` (the known trap).

## 11. Remaining limitations and risks

1. **No live network results.** Order discovery and network gap resolution are planned, tested and bounded, but not run (§4.4). Their recall against the real portal is unknown.
2. **Portal fixtures are derived.** Only two pages were really captured. The empty, blocked and later-page fixtures are derived from them. The real empty-result markup and Cloudflare page may differ, so `unrecognized_page` is the safe fallback. A supervised live run should capture real pages.
3. **`pv.promote` is unguarded for the ingestion callers** (`ncuc_loader`, `bulk_extractor`). The redline and date guards sit on the reviewer CLI and the D2 workflow only. Bulk ingestion can still promote a redline-derived run as before.
4. **Bar geometry is a heuristic.** Colour plus bar position covers the E-2 Sub 1300 compliance books and the E-2 Sub 1076 exhibit seen here. Redlines drawn in black, or as images, are not caught (`no_text_layer` is reported, not `clean`). The struck-suffix threshold (25%) is tuned on one page.
5. **`identity_struck` relies on case-exact text search.** A clue spanning lines is undecided (`None`), and is treated as not struck.
6. **Effective-date assessments trust the clue ledger's family-page attribution.** Bundles that print another sheet's date on a family page can create false conflicts. All 35 conflicts need review.
7. **Rehearsal approvals are not human reviews.** No proposal on any database has a human approval.
8. **GitNexus index lagged.** The incremental re-index segfaulted. A forced re-index then needed a 4 GiB buffer pool. Impact for D1/D2 modules before that was established by text search (§2.4). Two commits (`9ddd155`, `6645021`) got their `detect_changes` run after commit: risk **high**, flows limited to `resolve_gaps_local_cmd` and the new reconcile command.
9. **D1 remaining items not addressed:**
   - 40 bound PDFs without native page artifacts;
   - the 18 "other-leaf" bindings;
   - D1's clean-copy-excludes-anchor limitation.

## 12. Recommended next milestone (D3) [Proposed]

1. **Supervised live run** (with your authorization): 10 explicit `portal_orders` queries. Capture real result, empty and blocked pages as fixtures. Retrieve and review the top order candidates.
2. **Production corrections through the D2 workflow**, approved by a person: hd 7265 (metadata date, then rebind, then promote) and hd 2275 (redline duplicate).
3. **Guard the ingestion promotion path:** a cheap assertion and page-evidence check in `bulk_extractor` / `ncuc_loader`, with its own CRITICAL impact review.
4. **Review queue for the 35 date conflicts and 45 redline-page candidates.** Then D5 (inserted/deleted value reconstruction), seeded by `struck_samples` / `inserted_samples` and D1's `redline_ambiguous` clues.
5. **Bind the verified on-disk sheets** (D1's 16 verified gaps) through a `bind_unbound_document` proposal kind. It reuses the same checks and snapshots.
