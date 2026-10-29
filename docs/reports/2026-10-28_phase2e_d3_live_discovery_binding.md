# Phase 2E D3: Live Discovery Validation, Ingestion Safety and Verified Document Binding

**Date:** 2026-10-28
**Branch:** `phase2e/d3-live-discovery-binding`, cut from `phase2e/d2-portal-orders-promotion` @ `e9e6b9e`. Not pushed or merged.
**Plan and acceptance criteria:** `docs/design/2026-10-28_phase2e_d3_plan.md` (brief §8 criteria made measurable; the file was written mid-session, after the pilot, see its sequence note).
**Data:** one scratch byte copy, `d3.db` = D2's `d2.db` (production + D1/D2 ledger), migrated to provenance v5. Disk had 20 GB free, so one copy served the pilot, rehearsal, queue and benchmark; the rehearsal was unwound byte-identically before the benchmark ran.
**Artifacts:** `reports/phase2e_d3/`, `tests/fixtures/d3_portal/`.

Evidence labels (as in D1/D2):
- **[Measured]**: counted on scratch or on the live portal this session.
- **[Assistant page review]**: read from a rendered page image (or the text layer, where stated) by the assistant this session. **Not a human review.**
- **[Rule]**: machine rule output.
- **[Unverified]**: a candidate nobody has reviewed.
- **[Proposed]**: future work or a correction not applied.

No human review was performed on any item in this report. Every approval in the rehearsal was made by a rehearsal actor (`d3-rehearsal-*`) on scratch.

---

## 0. Summary

| Acceptance criterion | Result |
|---|---|
| L1 ≤ 10 live searches, all ledgered | **Met.** 10 distinct queries (9 DocumentParamSearch + 1 docket inventory), 10 `search_attempts` rows with params, stop reason, pages, counts. 2 document downloads of listed orders. |
| L2 no bypass, no uncontrolled retry | **Met.** Login through the existing integration (stored credentials, no interactive step). No challenge, no block, no retry. |
| L3 retrieval ≠ relevance | **Met.** Both downloads read against their own pages; relevance reported separately (§4). |
| V1 all pathways assessed | **Met.** 3 `pv.promote` callers and 8 direct `tariff_charges` writers (§6.1). |
| V2 contamination cannot bypass | **Met.** 14 tests over bulk legacy replace, the NCUC loader, direct `pv.promote`, the reviewer CLI and the LLM promotion path. |
| V3 compatibility | **Met.** Clean sources promote unchanged. 11 legacy promotion tests now use a real one-page PDF instead of placeholder bytes. The parser-profile scaffold bypasses validation explicitly (§6.4). |
| B1 ≥ 3 rehearsed bindings | **Met.** 4 applied (3 `create_version`, 1 `replace_redline_source`); 6 correctly blocked. |
| B2 rollback | **Met.** All 7 touched tables byte-identical after unwind (pass 2). |
| B3 gap status | **Met.** `verified → resolved` only after the post-apply read-back; restored on rollback. |
| E1 35 conflicts | **Met.** 35/35 classified by rule; 7 carry a proposed correction; 0 dates written. |
| T1 tests | See §12. |
| T2 benchmark | See §12. |
| P1 production | **Met.** SHA-256 unchanged (§14). |

**Headline findings:**
1. **Docket-scoped DocumentParamSearch is not usable for orders** [Measured]. 9/9 live ORDER queries printed "** NO RESULTS FOUND **", including the E-7 Sub 1026 general rate case. The same docket's DocketDetails inventory lists 100 documents on page 1 alone, 33 of them orders. D2 would have recorded all 9 as completed negative evidence. The repository's own `docs/NCUC_PORTAL_WORKING_METHOD.md` already said docket-scoped doc-param search returns zero rows for real dockets. D2 did not consult it.
2. **The local archive already held one of the two orders the pilot retrieved** [Measured]. Commission orders are captioned in several dockets but recorded under one. A caption-docket index of the 730 local order files gives **84 of 113** order gaps a local candidate, with no network request (was 0).
3. **65 of 411 versions with billed charges rest on pages carrying redline markup** [Rule]. 45 have strong signals (glued revision text such as "FifthSixth", or ≥3 struck spans), and those 45 carry **1,113 charges**. Unattended re-promotion of these versions is now refused.
4. **A billed EDPR charge's sign disagrees with the order that set it** [Assistant page review]. Version 6469 stores +0.0058 ¢/kWh. The 2017 order and the sheet's own p.3 make it a decrement (0.0058); the sheet's p.2 prints it unsigned.
5. **11 of the 35 date conflicts are "observed period held by a sibling"** [Rule]. Another version already starts on the printed date. Those are duplicate registrations or wrong bindings, not date errors; "correcting" them would create two versions for one period.

## 1. Implementation summary

| Work item | Files | What it does |
|---|---|---|
| Pilot controls | `cli_commands/provenance.py` (`run-network-searches`) | `--query-hash` (explicit query selection) and `--capture-dir` (the page each search ended on, plus its outcome) |
| Empty docket search | `historical/ncuc/document_param_search.py` | `docket_scoped_empty` stop reason: incomplete, retryable, never negative evidence. `search()` unchanged. |
| Docket inventory channel | `historical/ncuc/remote_discovery.py` 1.1.0 | Order gaps planned as `portal_orders` with `method=docket_inventory` (one query per docket; 103 → 88). Resolve DocketId exactly; parse the `documentsGridView` grid by its labelled cells; keep order rows; pager → `inventory_first_page_only`. |
| Local order captions | `historical/ncuc/local_gap_resolution.py` 1.3.0 | `order_caption_index`: dockets printed in each local order's caption (pages 1–2, text layer). |
| Shared ingestion validation | `historical/ncuc/ingestion_validation.py` (new), `db/provenance.py` (`promote`), `bulk_extractor.py`, `ncuc_loader.py`, `llm_charge_promotion.py` | Source checks inside `pv.promote` for tariff-version and rider runs; pathway policy; named, recorded overrides; `audit_version_sources`; `version_source_redline` for writers without a run. |
| Validation fixes found by the audit | `clue_ledger.file_identity`, `parse/redline_detector.evaluate_redline_page` | Cache keyed by size+mtime (was path only). An underline bar under black text is `suspect`, not `redline`. |
| Verified binding | `historical/ncuc/document_binding.py` (new), `document_promotion.py` | `bind_unbound_document` kind: attach / replace_redline_source / create_version, chosen by checks; post-apply validation; gap → resolved; exact rollback. |
| Review queue | `historical/ncuc/evidence_review.py` (new) | `evidence_review_items`, built from five sources; decisions kept on rebuild. |
| Date conflicts | `historical/ncuc/effective_date_reconciliation.py` | `classify_conflicts`: nine causes, page evidence, proposed corrections only. |
| Schema v5 | `db/discovery_review_schema.py`, `db/provenance_schema.py` | `promotion_validations`, `evidence_review_items`, proposal kind widened (table rebuild, rows/ids kept), `promotion_proposals.historical_document_id` nullable. |
| CLI (6 new) | `provenance` | `propose-binding`, `build-review-queue`, `review-queue`, `review-item`, `classify-date-conflicts`, `audit-version-sources`. Snapshot 371 → 377, updated deliberately. |
| Rehearsal tools | `bench/clues/binding_state.py`, `bench/clues/binding_rehearsal.py` | Table fingerprints; scripted end-to-end binding rehearsal and unwind. |
| Tests | `test_d3_portal_inventory.py` (7), `test_d3_ingestion_validation.py` (14), `test_d3_document_binding.py` (10), `test_d3_review_queue.py` (8) | 39 new tests. |

**Reused, not duplicated:** the D2 proposal lifecycle and snapshots; `_identity_checks` and `read_pages`; `PageRedlineReader` / `summarize_page_evidence`; the D1 stored-fingerprint lookup (`_redline_fingerprint`); `effective_date_promotion_conflict`; `session.resolve_docket_ids` / `get_docket_documents` / `download_view_file`; `SearchLedger`; `pv.rollback`; `provenance extract-candidate`; billing's `select_version_with_coverage` (approved-only) as the reason created versions are not billable.

## 2. Live NCUC search pilot [Measured]

### 2.1 Selection

Explicit order gaps first, then distinct docket types: a DEC general rate case, DEC rider dockets, an old DEC docket, a commission-wide `E-100` docket with a printed-date window, an `M-100` docket (no company filter), and DEP dockets with and without a window. The 10th query was held back to test the hypothesis the first nine raised.

### 2.2 Every attempt (`reports/phase2e_d3/pilot/pilot_attempts.json`)

| # | Attempt | Docket | Company filter | Window | Method | Stop reason (as recorded live) | Pages | Rows | Time |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 1478 | E-7 Sub 1026 | DEC | — | DocumentParamSearch, ORDER | empty_result | 1 | 0 | 12 s |
| 2 | 1479 | E-7 Sub 989 | DEC | — | 〃 | empty_result | 1 | 0 | 13 s |
| 3 | 1480 | E-7 Sub 831 | DEC | — | 〃 | empty_result | 1 | 0 | 13 s |
| 4 | 1481 | E-7 Sub 487 | DEC | — | 〃 | empty_result | 1 | 0 | 13 s |
| 5 | 1482 | E-100 Sub 211 | DEC | 09/14–09/28/1981 | 〃 | empty_result | 1 | 0 | 11 s |
| 6 | 1483 | M-100 Sub 138 | none | — | 〃 | empty_result | 1 | 0 | 14 s |
| 7 | 1484 | E-2 Sub 1023 | DEP | — | 〃 | empty_result | 1 | 0 | 12 s |
| 8 | 1485 | E-2 Sub 1153 | DEP | 12/10–12/24/2020 | 〃 | empty_result | 1 | 0 | 13 s |
| 9 | 1486 | E-2 Sub 1304 | DEP | — | 〃 | empty_result | 1 | 0 | 13 s |
| 10 | 1487 | E-7 Sub 1026 | — | — | **DocketDetails inventory** | inventory_first_page_only | 1 of 6 | 100 (33 orders) | 12 s |

Downloads: 2, both orders listed by attempt 1487 (§4). Login: once per command (3 commands), all through `session.create_authenticated_context` with the configured credentials; no interactive step was needed.

The filter was verified on every DocumentParamSearch (`filter_verified = 1`): the ORDER filing type was applied and read back. The portal itself printed "** NO RESULTS FOUND **" on all 9 pages, and the empty statement was read correctly. The queries matched nothing.

### 2.3 What was recorded, and the correction

The live run recorded attempts 1478–1486 as `no_results / empty_result`: completed negative evidence that would have stopped any retry. After attempt 1487 showed the docket has orders, those nine rows were reclassified on scratch to `incomplete / docket_scoped_empty`, with an `error_detail` note saying so. The code now classifies a docket-filtered empty page this way when it happens. No other attempt row was edited.

### 2.4 Discovery effectiveness

| | Value |
|---|---|
| Queries with any result | 1 of 10 |
| Distinct order documents listed | 33 (page 1 of 6 only) |
| Candidate rows | 1,419 (33 orders × 43 targets the docket serves: one row per target, as in D2) |
| Rows with an empty description from `session.get_docket_documents` | 100 of 100 (fixed by the grid parser; scratch rows re-derived from the captured page, offline) |
| Targets whose order is on the page read | 2 (EDPR 2017 and 2024). The 18 explicit order gaps (DEC schedules effective 2013-11-01) need the 2013 rate-case orders on pages 2–6, which were not read. |

## 3. Portal reliability observations [Measured]

1. **No blocking.** No Cloudflare page, no 403, no throttling in 10 queries and 2 downloads over about 11 minutes.
2. **DocumentParamSearch with a docket number returns nothing for real dockets.** That held with and without a company filter, with and without a date window, and for E-, M- and E-100 dockets. The filter was verified each time, so this is how the portal answers the query, not a form fault.
3. **The DocketDetails inventory pages.** Page 1 holds 100 rows, newest first, and its pager links pages 2–6. `session.get_docket_documents` reads only the page it lands on. The outcome now says so (`inventory_first_page_only`, `pages_total = 6`), so the attempt stays retryable. Paging the inventory (or its per-page box) is not implemented: no live evidence of how its postback behaves was gathered within the 10-query limit.
4. **Real pages captured** (`tests/fixtures/d3_portal/`, hidden ASP.NET state and scripts blanked): the real empty-result page and the real inventory page. They replace D2's derived fixtures for these two states.

## 4. Documents and orders discovered; identity verification

| Candidate | Listing | Downloaded | Identity (rule, own pages) | Relevance [Assistant page review] |
|---|---|---|---|---|
| 103 | "Order Approving EDPR Rider", filed 6/12/2024 | yes, 69 KB, SHA-256 `a5f445bb…` | docket printed ✓, order wording ✓ | Captioned E-7 Subs 487/828/1026/1146/1214/1276. EDPR decrement **(0.0027) ¢/kWh effective July 1, 2024** = version 5239's stored −0.000027 $/kWh. **Already in the archive**, byte-identical, as an E-7 Sub 1214 order record. |
| 1374 | "Order Approving EDPR Rider", filed 6/20/2017 | yes, 118 KB, SHA-256 `2974f7d2…` | docket printed ✓, order wording ✓ | Captioned E-7 Subs 487/828/1026. EDPR decrement **(0.0058) ¢/kWh effective July 1, 2017**: the period of version 6469, whose stored charge is **+**0.000058 (§9 item 1). It also prints the **September 24, 2013** E-7 Sub 1026 rate-case order date. New to the archive. |

Both candidates stay `needs_review`: neither was linked, because linking needs a human reviewer. The two files are in the scratch download folder only.

## 5. Discovery improvements made from the pilot

| Change | Evidence | Effect [Measured] |
|---|---|---|
| `docket_scoped_empty` is incomplete, not `no_results` | 9/9 empty pages vs 33 orders in the inventory | Prevents false negative evidence. Also covers `portal_params` docket queries and `missing_clean_doc_search`. |
| Orders via docket inventory, one query per docket | Attempt 1487 | Order queries 103 → 88 (plan over D2's gap set). Date windows rank rows instead of filtering them. |
| Grid parser for DocketDetails | 100/100 empty descriptions | 100/100 rows with type, description, date and links. |
| Order-row filter | "Compliance Tariffs Pursuant to … Order" is a filing | 33 orders / 67 filings on the page. Tested. |
| Caption-docket local order search | Order 103 archived under another docket | Order gaps with a local candidate: **0 → 84 of 113**, 433 candidates (all `partial`; which order a version cites is not established). 38 s for 730 files, no network. |
| `--query-hash`, `--capture-dir` | Pilot needed explicit selection and real pages | Supervised runs choose their queries and leave fixtures. |

Not changed for lack of evidence: query specificity beyond the docket, `E-100` company filtering (the empty result is explained by the docket-scoped search itself), inventory paging.

**Network plan after D3** [Measured on `d3.db`, `network_plan_after.json`, after the full resolver 1.3.0 rerun]:

| Channel | D2 | D3 |
|---|---:|---:|
| `portal_orders` | 103 (DocumentParamSearch, ORDER filter) | **58** (`docket_inventory`, one per docket) |
| `portal_params` | 53 | 53 |
| `zoom` | 134 | 134 |
| **Total** | **290** | **245** |

`searched_not_found` gaps: 249 → 165. The 84 order gaps with a local caption candidate moved to `candidate_found`. E-7 Sub 1026's inventory attempt is `incomplete`, so it stays planned for a paged retry.

## 6. Ingestion safety

### 6.1 Pathways assessed

| Pathway | Reaches `tariff_charges` / rider tables via | Could introduce (before D3) | D3 |
|---|---|---|---|
| `provenance promote` (reviewer CLI) | `pv.promote` | guarded in D2 (redline, date) | Same checks, now inside `pv.promote`; pathway `reviewed`; overrides named and recorded |
| `BulkExtractor._legacy_promote` (`DUKE_RATES_LEGACY_REPLACE`) | `pv.promote` | struck charges, wrong date, changed bytes: **unguarded** | Fails closed (`automated:bulk_extractor.legacy_replace`); refusal recorded on the result and in `promotion_validations` |
| `ncuc_loader._replace_through_runs` (rider summaries, legacy replace) | `pv.promote` | redline rider pages: **unguarded** | Fails closed on redline evidence pages, changed bytes, or no evidence pages (`automated:ncuc_loader.legacy_replace`); raises |
| Any other `pv.promote` caller | `pv.promote` | — | Default pathway `automated:direct`: fails closed |
| `promote_llm_charge_proposals` | direct INSERT | charges from struck pages | `version_source_redline` before insert → `skipped_source_redline` |
| `proposed_tariff_promoter` | direct INSERT into a version it creates | struck values from proposal redlines | **Assessed, not guarded** (§15): its input is proposal blocks with their own page refs, not a version's bound pages |
| `replace_parsed_tariff_data` (`tariff-update --auto-parse`) | DELETE + INSERT | history loss | Already refuses history loss (`HistoricalDataLossError`); utility web sheets; unchanged |
| `rebind_historical_page_range` (legacy) | DELETE | charge loss | D2 guard (`--allow-charge-deletion`); unchanged |
| `BulkExtractor.insert_charges` | DELETE + INSERT | — | No pipeline caller (Phase 2C); unchanged |
| `upsert_tariff_charge`, retire/delete helpers, `export_audit` | INSERT / DELETE | — | Not ingestion of extracted evidence; unchanged |

### 6.2 Checks and policy

| Check | Blocks unattended | Blocks reviewed | Overridable (reviewed only) |
|---|---|---|---|
| `source_present` | yes | no (reported) | yes |
| `source_hash_matches` (run bytes; archive SHA-256/MD5/SHA-1) | yes | yes | yes |
| `bound_pages_in_file` | yes | yes | no |
| `no_redline_duplicate_assertion` | yes | yes | yes |
| `bound_pages_not_redline` (page evidence **or** stored fingerprint) | yes | yes | yes |
| `effective_date_printed` | yes | yes | yes |
| `evidence_pages_known` (rider runs) | yes | no | — |

- A version sourced from a utility web document (no historical document) is `not_applicable` and passes.
- Overrides on an unattended pathway raise `ProvenanceError`.
- `IngestionValidationError` subclasses `ProvenanceError`, so existing `except ProvenanceError` handling (bulk extractor) records the refusal instead of crashing.

### 6.3 Shadow audit of existing charges [Rule; `source_audit_existing_versions.json`, 85 s]

The same checks run read-only over every version that already has charges:

| Result | Versions |
|---|---:|
| pass | 345 |
| `bound_pages_not_redline` | 64 |
| `bound_pages_not_redline` + `source_hash_matches` | 1 |
| `source_present` (file missing) | 1 |
| **Total** | **411** |

Redline signal grades (`source_audit_redline_grades.json`):

| Grade | Versions | Charges |
|---|---:|---:|
| strong: glued revision text or ≥ 3 struck spans | 45 | 1,113 |
| moderate: 1–2 struck spans | 7 | 113 total for the three lower grades |
| weak: coloured insertion only | 10 | |
| fingerprint only | 3 | |

Spot check: 6 flagged pages read [Assistant page review]. 5 were genuine redlines; 1 (v5239, a DSM workpaper) was a false positive. It led to the detector fix in §6.4.

Two defects the first audit pass exposed, both fixed and re-measured:
- **`content_hash` is often not a file hash.** First pass: 118 "mismatches". 66 of those are the file's **SHA-1**, 1 is a hash prefix, and the rest are UUIDs, `local://` ids or short codes. Only 64-, 40- or 32-hex values are now compared (SHA-1 included). 1 real mismatch remains.
- **Redline verdict on an underline bar under black text** (a box border) is now `suspect`. Regression test added.

### 6.4 Compatibility

- The default (non-legacy) bulk path, which stores candidate runs only, is untouched.
- Existing promotion tests whose fixtures used the placeholder bytes `%PDF-1.4 test leaf 500` with `content_hash='abc'`, or a rider source path that did not exist, were moved to a real one-page PDF with its true hash and a rider evidence page. Their assertions are unchanged, and they now exercise the pass path. New tests cover the fail-closed path.
- **The `legacy_replace_persistence` test scaffold** (`tests/conftest.py`) runs pre-2C parser-profile tests (`test_historical_parser_profiles.py`, `test_reprocess_queue.py`) through candidate → promote, with documents bound to placeholder paths. It failed 12 tests on `bound_pages_in_file`: there are no source pages. Inside that scaffold only, the shared validation is replaced by a visible `not_applicable` stub. Those tests check profile selection, not sources; validation itself is tested on real files.
- **Stale identity cache:** `clue_ledger.file_identity` cached by path only, so a file rewritten at the same path kept its old hash. Found by these fixtures; fixed (§13).

## 7. Verified document binding workflow

`provenance propose-binding --gap-candidate-id N [--target-version-id V] --by NAME` then `approve-proposal`, `apply-proposal`, `rollback-proposal` (D2 commands).

| Check | Blocking |
|---|---|
| File present, pages in file, **file is the reviewed file** (SHA-256 recorded at propose time) | yes |
| Family leaf printed on the first page; no other leaf in the run; utility; single revision identity | yes (D2 checks) |
| Printed revision = the gap's expected revision | yes |
| Exactly one printed effective date | yes |
| Page range complete (the next page does not continue the same revision) | yes |
| Pages not redline (page evidence or glued text) | yes |
| Pages not already bound to the family (path or hash) | yes |
| Binding mode allowed (below) | yes |
| Supersession predecessor on file; source provenance known (discovery record) | no (reported) |

| Mode | When | Writes |
|---|---|---|
| `attach` | one matching version, with no source document (e.g. a `pending_document` placeholder) | new `historical_documents` row; `tariff_versions.historical_document_id`; status **not** changed |
| `replace_redline_source` | one matching version whose bound pages are a redline | new row; the version points at it; the redline row is kept |
| `create_version` | no version prints the revision or starts on the printed date | new row + new version, status `bound_pending_charges` (billing selects only `approved`) |
| — (blocked) | a matching version already has a clean source; several match; a target that does not match | nothing |

At apply:
- checks re-run;
- the plan must be unchanged since approval;
- snapshots are taken;
- the bound row and its pages are read back (`post_apply_validation`), and only then does the gap move to `resolved`.

Rollback refuses while charges promoted onto the version are in place. It restores pointers, deletes the created rows, and restores the gap row exactly, including `updated_at`.

## 8. Scratch binding rehearsal [Measured; `reports/phase2e_d3/binding/`]

Pass 1 (manual CLI) found that gap rollback reset `updated_at`. That was fixed, and pass 2 ran scripted (`bench/clues/binding_rehearsal.py`) from a fresh baseline.

| Candidate | Sheet | Mode | Result |
|---|---|---|---|
| 461 | DEP Fourth Revised Leaf 600, eff. 2024-12-01 (E-2 Sub 1345 p.9–13) | create_version | applied: version 7317 (`bound_pending_charges`), 0 charges created/deleted, read-back ✓, gap 1634 → resolved |
| 399 | DEC Sixty-Second Revised Leaf 99, eff. 2024-09-01 | replace_redline_source on v6737 | applied: v6737 re-sourced from its redline to the clean sheet; its 46 charges kept |
| 417 | DEP First Revised Leaf 503, eff. 2024-10-01 | create_version | applied |
| 569 | DEC Fifty-Seventh Revised Leaf 55, eff. 2019-12-01 | create_version | applied |
| 459 | second copy of 461's sheet (E-2 Sub 1341) | — | **blocked**: resolves to the new version 7317, which has a clean source |
| 570 | second copy of 569's revision, but it prints 2019-11-16 | — | **blocked** (same); the two printed dates differ and need review |
| 483, 576 | Leaf 607 / 608 | — | **blocked**: a version already starts on the printed date with a clean source |
| 540 | Leaf 704 | — | **blocked**: hd 1592 is the source of two versions in two families (review item) |
| 554 | DEC Second Revised Leaf 226 | — | **blocked**: `pages_not_redline` (the D2 redline exhibit) |

End to end on candidate 461:
1. `extract_candidate` over the bound pages gave run 2: **125 charges, 14 TOTAL rows** (the same shape as D1/M1.1's Leaf 600 rebind).
2. Promoted through the **automated** pathway: validation `pass`, 125 inserted. The version stayed `bound_pending_charges`, so billing still did not select it.
3. `rollback-proposal` was refused while those charges were active.
4. `provenance rollback` then the four proposal rollbacks.

After unwind, every fingerprinted table is **byte-identical** to the baseline: `tariff_versions`, `historical_documents`, `tariff_charges`, `lineage_links`, `lineage_gaps_typed`, `document_assertions`, `effective_date_assessments`. `tariff_charges` and `lineage_links` were also unchanged while the bindings were applied. Billing selection for the affected families at the new dates was the same before and after apply.

## 9. Evidence review queue [`reports/phase2e_d3/review_queue.json`]

`provenance build-review-queue` built **166 items**, all `open`. 26 carry assistant page review; none are human-reviewed.

| Priority | Problem type | Items | Charges |
|---:|---|---:|---:|
| 1 | `billed_charges_on_redline_source` | 52 | 1,154 |
| 1 | `effective_date_conflict` (version billable) | 28 | 422 |
| 1 | `charge_sign_conflict` | 1 | 1 |
| 2 | `billed_charges_on_redline_source` (weak / fingerprint-only) | 13 | 72 |
| 2 | `effective_date_conflict` (not billable) | 7 | 0 |
| 2 | `verified_document_unbound` | 17 | — |
| 2 | `document_bound_to_two_families` | 1 | 0 |
| 2 | `local_candidate_on_redline_pages` (verified identity) | 1 | — |
| 3 | `local_candidate_on_redline_pages` | 44 | — |
| 3 | `order_identified_for_version` | 2 | 2 |

Every item carries:
- problem type, family, version, historical document;
- source documents with paths, hashes and pages;
- printed evidence;
- database values;
- an explanation, and a cause where one is classified;
- a proposed correction (never applied);
- the evidence level;
- downstream impact (charges, billability, the service-date window a date change would move);
- review status.

`review-item` needs a reviewer name and a note. Rebuilds keep every decision and a human evidence level. The 17 unbound verified documents: 11 are ready to bind (10 create, 1 replace), 6 blocked with reasons.

Page-reviewed findings (`review_manual_findings.json`) [Assistant page review]:
1. **v6469 EDPR, sign.** The sheet p.2 prints "Change in Rates 0.0058 ¢/kWh" unsigned; p.3 prints "(0.0058)"; the order says decrement (0.0058). Stored +0.000058 $/kWh. Proposed correction: −0.000058, through a reviewed candidate run.
2. **v5239 and v6469 orders identified.** `order_date` is NULL on both. Proposed: read each order's signature page, then `revise-metadata`.
3. **hd 1592 is the source of v6110 (`nc-progress-leaf-704`) and v6156 (`ncuc-dep-704`).** This blocks candidate 540.

## 10. Effective-date conflict classification [Rule; `date_conflicts_classified.json`]

| Cause | n | Correction proposed | Meaning |
|---|---:|---:|---|
| `observed_period_held_by_sibling` | 9 | 0 | another version starts on the printed date: duplicate registration or wrong binding |
| `bound_document_from_other_period` | 6 | 0 | printed date > 1 year away: the pages are another revision's sheet |
| `redline_contamination` | 4 | 0 | the printed date is on redline pages |
| `stored_filing_or_order_date` | 4 | 4 (medium) | stored = the document's filing/letter date, within 400 days before the printed date (incl. **6548**: 2024-01-24 → 2025-01-01) |
| `unexplained` | 4 | 0 | — |
| `proposed_or_unapproved_sheet` | 3 | 0 | "(Proposed)" revision or blank "Order dated": the date was requested, not approved |
| `version_date_disagrees_with_its_document` | 2 | 2 (high) | the document row agrees with the print; only the version row differs |
| `adjacent_rate_year` | 2 | 0 | 1st-of-month dates a year apart (multi-year rate plan bundles) |
| `stored_year_digit_error` | 1 | 1 (high) | **5990**: 2026-10-13 → 2009-10-13 |
| **Total** | **35** | **7** | |

Rule refinements made after reading pages, each now tested:
- The first year-digit rule matched four 1st-of-month pairs by coincidence; it now requires a distinctive day.
- 5231 was first rated "high". Its page image shows "Sixth (**Proposed**) Revised Leaf No. 64" with a blank order date, which led to the `proposed_or_unapproved_sheet` cause.

[Assistant page review] confirmations:
- 5990: footer image "Effective October 13, 2009".
- 6052: text layer p.52 "Effective … January 1, 2021", Schedule TSS-65.
- 5231: proposal, so no correction.

No `tariff_versions` date was written. Most notable for billing [Rule]: **v6057** (Leaf 600, 136 charges) is stored 2025-01-16 but prints 2025-04-01, and **v5357** already holds 2025-04-01. Bills from 2025-01-16 to 2025-03-31 select 6057, a sheet effective only from April.

## 11. Before / after metrics [Measured on `d3.db`]

| Metric | D2 end | D3 |
|---|---:|---:|
| Live portal queries ever run | 0 | 10 |
| Order gaps with a local candidate | 0 / 113 | 84 / 113 |
| Planned network queries (orders) | 290 (103) | 245 (58) |
| Gap status `resolved` | 0 | 0 on scratch after unwind (4 during rehearsal) |
| Promotion pathways under source validation | 1 (CLI) | all `pv.promote` callers + LLM path |
| Versions with charges audited against their source | 0 | 411 (65 on redline pages) |
| Date conflicts classified | 0 / 35 | 35 / 35 (7 proposed corrections) |
| Review items with page-level evidence | 0 | 166 |
| CLI commands | 371 | 377 |
| Tests | 1,818 passed | 1,857 passed, 0 failed |

Resolver 1.3.0 on all gaps [Measured, `resolve_local_v13.json`, 2 min 34 s]:
- page-searchable results are **identical to D2's 1.2.0**: 87 full, 102 partial, 39 on redline pages, 6 identity-struck. The redline-verdict narrowing changed no candidate.
- the 113 order gaps were re-searched with caption dockets.
- the summary's `resolved_gaps: 16` is a D1 label that counts `verified` + `resolved`; **no gap is resolved** on scratch after the unwind.

## 12. Tests and benchmark

All runs use `.venv312` (Python 3.12).

| Run | Result |
|---|---|
| New D3 tests (`test_d3_*`) | 39 tests, all pass |
| Promotion / provenance / rider / bulk / D1 / D2 suites | 173 pass after the fixture changes in §6.4 |
| LLM promotion suites | 40 pass |
| `test_cli_command_surface.py` | passes after the deliberate snapshot update (371 → 377) |
| **Full suite** (`reports/phase2e_d3/pytest_full.txt`) | **1,857 passed, 0 failed**, 92 skipped, 4 xfailed (12 min 3 s). D2: 1,818 passed, 0 failed. |

The first full run (12 failures) and the fix are described in §6.4. An earlier run was stopped because it had started before later edits.

**Benchmark** (`bench/run_extraction.py`, 26 documents, run on `d3.db` after the rehearsal was unwound; `reports/phase2e_d3/bench/diff_vs_d2.txt`):

| Configuration | Verdict vs D2 |
|---|---|
| committed cache: legacy / docling / native / hybrid | **IDENTICAL** / **IDENTICAL** / **IDENTICAL** / **IDENTICAL** |
| production artifacts (`--docling-source db`): hybrid / docling | **IDENTICAL** / **IDENTICAL** |

This is expected: D3 changes no extraction, segmentation or billing code. The frozen billing baseline was not re-run for the same reason. Billing selection was checked directly for the families the rehearsal touched (§8).

## 13. GitNexus impact assessment

| Symbol | Risk | Callers / flows | Handling |
|---|---|---|---|
| `provenance.promote` | **CRITICAL** | 3 direct (provenance CLI, `ncuc_loader._replace_through_runs`, `BulkExtractor._legacy_promote`); flows `mine_tariff_sheets_nc`, `ingest_ncuc`, `mine_docling_nc` | Keyword-only additions with defaults; a clean source passes unchanged; 173 promotion/provenance/rider/D1/D2 tests plus 14 new |
| `BulkExtractor._legacy_promote` | HIGH | flows `mine_tariff_sheets_nc`, `run_nc_missing_doc_workflow_cmd`, `mine_docling_nc` | Only the pathway name added; refusal surfaces as `result.error` (existing handling) |
| `clue_ledger.file_identity` | HIGH | 4 direct; `resolve_gaps_local_cmd`, `binding_check_cmd`, `build_clue_ledger_cmd` | Cache key adds size + mtime; output unchanged for unchanged files |
| `_replace_through_runs` | LOW | `ingest_ncuc`, `load_ncuc_ingest` | Pathway name added |
| `promote_llm_charge_proposals` | LOW | `run_llm_promotion_overnight` | Redline skip added; its 40 tests pass |
| `evaluate_redline_page` | LOW (index) | via `PageRedlineReader` → resolver, D2 promotion checks, validation | Verdict narrowed for inserted-only on black text |
| `search_with_outcome`, `run_network_searches_cmd`, `remote_discovery.*`, `document_promotion.*` | not in index (D2 code; index 4 commits behind) | text search: CLI + tests; `search()` callers see no change | D2 tests pass (1 updated deliberately for the new order channel) |

`detect_changes` (all, before the first commit): risk **critical**, 39 symbols / 19 files. Affected flows are the portal search flows (`search doc-param`, `ncuc portal-search`, harvest; classification only), `resolve_gaps_local`, `binding_check`, `build_clue_ledger`, `migrate`, `promote`. `history_cmd` / `rollback_cmd` were "touched" by line shifts only.

## 14. Production safety verification

Production `data/db/duke_rates.db` (`reports/phase2e_d3/production_sha_{before,after}.txt`):
- SHA-256 `fe563a78…cd08` before and after; mtime 2026-10-26 12:36:11 both times; `-wal` 0 bytes.

**Identical.**

- No command in this session was pointed at production. Every write ran on `d3.db`.
- The 10 live queries and 2 downloads wrote only to `d3.db` and the scratchpad. The two downloaded orders are not in `data/`.
- The new CLI write commands use the existing production refusal (`--allow-production`).

## 15. Remaining risks and limitations

1. **No human review anywhere.** The 18 "verified" candidates, the rehearsal approvals, and every [Assistant page review] are not human decisions. The binding workflow treats candidate review state as evidence, not as a check.
2. **Inventory paging is not implemented.** E-7 Sub 1026 pages 2–6, which hold the 2013 rate-case orders behind 18 explicit gaps, were not read. Each inventory attempt is correctly incomplete and retryable.
3. **DocumentParamSearch with a docket is now never negative evidence.** That is safe, but `portal_params` docket queries (53 planned) will need the inventory route too. That change was not made without live evidence.
4. **Redline detection precision.** It is about 5/6 on the audited sample. The 10 weak and 3 fingerprint-only versions need page review before any action. Black-ink or image-only redlines are still not detected: `no_text_layer` is reported, not blocked, for scanned pages without a fingerprint.
5. **Unattended legacy replace is stricter.** It now refuses 66 of 411 versions with charges (65 redline + 1 missing file), and rider-summary runs without evidence pages. This is the requested fail-closed behaviour, but a legacy-replace batch will now report refusals where it used to promote.
6. **`proposed_tariff_promoter` is not guarded** (§6.1). Its blocks come from proposal filings, which are often redlines. Recommended next.
7. **Created versions need an explicit approval step.** `bound_pending_charges` versions never bill; moving one to `approved` after its charges are promoted has no workflow yet.
8. **Date-conflict rules are heuristics.** Each was checked on a few pages only. The 400-day filing lag and the rate-year band are tuned on this archive.
9. **Candidate rows multiply by target.** 1,419 rows for 33 orders. Harmless, but a docket serving many targets lists every order once per target.
10. **Parser-profile tests bypass source validation** through the test scaffold (§6.4). A parser change in those tests is not also a validation test.
11. **One scratch copy.** Disk space allowed only one 5.9 GB copy. Every rehearsal write was unwound and verified before the benchmark.

## 16. Recommended next milestone (D4) [Proposed]

1. **Human review of the priority-1 queue**, starting with:
   - the 45 strong redline-sourced versions (1,113 charges);
   - v6057/v5357 (Leaf 600, Jan–Mar 2025 billing);
   - the v6469 sign.

   Then apply the accepted corrections through the existing workflows (`revise-metadata`, `bind_unbound_document`, `mark_redline_duplicate`, reviewed candidate promotion), with `--allow-production` and a named reviewer.
2. **Inventory pagination** (`documentsGridView` `Page$N` postbacks, or the per-page box), validated in a second supervised run of ≤ 10 dockets, starting with E-7 Sub 1026 pages 2–6. Then move `portal_params` docket queries to the inventory route.
3. **Bind the 11 ready verified documents on production** after human approval, and add the `bound_pending_charges → approved` step.
4. **Guard `proposed_tariff_promoter`** with the shared page checks over its block pages.
5. **D5 value reconstruction** from redline pages (struck vs inserted values), seeded by the 45 strong redline versions.
