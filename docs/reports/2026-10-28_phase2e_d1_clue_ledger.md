# Phase 2E D1: Regulatory Clue Ledger and Local-First Gap Resolution

**Date:** 2026-10-28
**Branch:** `phase2e/d1-regulatory-clue-ledger`, cut from `phase2e/m11-fallback-correctness` @ `c652f0a`. Not pushed or merged.
**Design:** `docs/design/2026-10-28_next_document_discovery_milestone.md`. Deviations are listed in §2.3.
**Data:** scratch byte copies of production only.
- `d1.db`: ledger, links, gaps, resolution and benchmark.
- `d1_rebind.db`: rebinding rehearsal.

Production was never opened for writing; its SHA-256 is unchanged (§12).
**Artifacts:** `reports/phase2e_d1/`, `bench/clues/`.

Evidence labels:
- **[Measured]**: counted on scratch this session.
- **[Hand-verified]**: read from a rendered PDF page image, not from the text layer.
- **[Rule]**: machine rule; never presented as human verification.

---

## 0. Summary

| Definition of done | Status |
|---|---|
| Regulatory clues persisted with verifiable source pages | **Done.** 30,027 clues. Every one is the exact `[char_start:char_end]` slice of the page text it cites (30,027/30,027 checked), with file SHA-256, page, artifact id, page-text SHA-256 and bbox where a text layer exists. |
| Supersession references generate explicit lineage candidates | **Done.** 201 links from 186 versions whose own sheet prints a supersedes reference. Every such version has ≥1 link. |
| Verified relationships distinct from inferred | **Done.** Link status: `verified` 45 / `candidate` 5 / `conflict` 10 / `explicit_unresolved` 141. Separate `human_review` column: **10 links hand-confirmed against page images, 0 rejected**. No link comes from date adjacency. |
| Missing-document hypotheses as typed gaps | **Done.** 691 active gaps across 8 types, each `explicit` or `inferred`, each with supporting clue ids. |
| Local archive searched before network | **Done.** 370 local searches: 122 gaps with on-disk candidates (92 full, 103 partial matches). 16 gaps `verified` by hand review. **0 gaps resolved**: nothing was bound. |
| Search attempts recorded and deduplicated | **Done.** 370 `search_attempts`. An identical second run made **0** searches and avoided **370**. Failed / blocked / incomplete attempts never suppress a retry. |
| Index-page binding corrections validated on scratch | **Done, with a finding.** hd 7265 → pp.96–101: 125 charges, **14/14 printed TOTALs reconcile**. hd 2275 → pp.100–105 is a **redline**: the candidate extracts 11 struck-through RAL-2 rows and fails 11/14 TOTALs. It must not be promoted (§10). |
| Extraction and billing unchanged | **Done.** 26-document benchmark **IDENTICAL** to M1.1 in all 6 configurations, including evidence pages. No extraction or billing code changed. |
| Production untouched | **Done.** SHA-256 identical before and after. |

**Headline findings** (beyond the deliverables):
1. The existing `heuristics.SUPERSEDES_LEAF_RE` **cannot match DEC's "Superseding North Carolina … Leaf No."** form (≈3,400 occurrences in the archive). That is a large part of why only 5 of 321 stored supersedes labels resolved in M2.
2. **The 1b72e8de E-2 Sub 1300 bundle is a redline that the redline detector does not flag** (`is_redline_candidate = 0`). Its text layer fuses deleted and inserted values ("First RevisedOriginal", "20243", glued TOTALs).
3. **Several "missing" sheets are already on disk.** For example, DEP Fourth Revised Leaf 600 (effective 2024-12-01), whose absence the family's lineage reported, is in the archive (hand-verified, §8.2).
4. **The binding check found 18 more spans** that print only *other* leaf numbers, besides the two index-page bindings. They are listed for review, not asserted as defects.

## 1. Implementation summary

| Piece | File | What it does |
|---|---|---|
| Schema (provenance v3) | `src/duke_rates/db/clue_ledger_schema.py`, `provenance_schema.py` | 7 tables, installed only through `provenance migrate`. Drop refuses when human reviews or search history exist. |
| Page clue extractor | `historical/ncuc/regulatory_clues.py` | Pure function `extract_page_clues(text)`. Docket, order date, effective date, leaf, schedule code, revision, supersedes, approval reference, utility. Exact offsets. Redline glue / blanks / OCR repairs are labelled, never normalised silently. |
| Ledger builder | `historical/ncuc/clue_ledger.py` | Reads native page artifacts for bound spans. Verifies file identity and page integrity. Locates bboxes (PyMuPDF text layer). Writes clues, scanned pages, and span-based associations. Includes the A3 checker and the read-only bound-span check. |
| Links and typed gaps | `historical/ncuc/clue_lineage.py` | Version identity from clues. Supersession links with stored rule checks. Eight gap types. Rebuild keeps review columns and advanced gap statuses. |
| Local resolution | `historical/ncuc/local_gap_resolution.py` | One pass over native page text indexes revision/schedule identities. Per gap: candidate page runs with per-criterion match, clue ids and rationale. Records search attempts. |
| Search ledger | `historical/ncuc/search_ledger.py` | Canonical parameter hashing, outcome classification, retry policy. |
| Searcher integration | `historical/ncuc/missing_clean_doc_search.py` | Optional `search_ledger=` argument records one attempt per portal / public-search query. Default behaviour unchanged. |
| CLI | `cli_commands/provenance.py` | `build-clue-ledger`, `typed-gaps`, `resolve-gaps-local`, `binding-check`, `search-ledger`. All take `--db` and refuse production unless `--allow-production`. |
| Benchmark | `bench/clues/` | `select_sample.py`, `render_pages.py`, `transcription.yaml` (hand transcription), `build_gold.py` → `sample_50.yaml`, `score_clues.py`, `rebind_check.py`. |
| Tests | `tests/test_d1_clue_ledger.py` | 36 tests (§12). |

Reproduce on a scratch copy:

```
duke-rates provenance migrate --db SCRATCH
duke-rates provenance build-clue-ledger --db SCRATCH --json reports/phase2e_d1/clue_ledger_build.json
duke-rates provenance typed-gaps --db SCRATCH --json reports/phase2e_d1/typed_gaps.json
duke-rates provenance resolve-gaps-local --db SCRATCH --by <name>        # run twice: 2nd run avoids every search
duke-rates provenance binding-check --db SCRATCH --json reports/phase2e_d1/binding_check.json
python bench/clues/build_gold.py && python bench/clues/score_clues.py --db SCRATCH
```

## 2. Existing infrastructure reused

### 2.1 Reused

| Existing | How D1 uses it |
|---|---|
| `heuristics.NCUC_FOOTER_RE`, `SUPERSEDES_LEAF_RE` / `SUPERSEDES_SCHEDULE_RE`; `nc_carolinas._REVISION_RE` / `_EFFECTIVE_RE`; `historical.metadata` patterns | The clue patterns generalise these, and a test asserts the old extractors' matches remain a subset. The old functions return only the first match and drop offsets, so they could not be called directly. |
| `ncuc_page_artifacts` (`page_miner_v*`) | The only text source. No PDF conversion anywhere. |
| M1 page-integrity rule | Applied to native artifacts: rows must carry the file's SHA-256 or MD5, and may not cite a page beyond the PDF page count. `docling_page_miner_v1` is excluded outright. |
| `document_fingerprints` (redline detector) | Recorded as document context on clue associations, and blocks a `full` local candidate. |
| `tariff_families.tariff_identifier` / `schedule_code`, `historical_documents.leaf_no` | Family leaf and schedule base, e.g. `nc-carolinas-rider-SUMMARY` → leaf 99; `RIDER_JAA_RY1` → `JAA`. |
| `ncuc_discovery_records` (`filing_classification='order'`) | Local search for missing-order gaps. |
| Provenance schema migration / rollback / production guard | D1 tables are schema v3 of the same opt-in module. |
| `lineage rebind-historical-page-range` | Used unchanged for the rehearsal. |
| `provenance extract-candidate` | Candidate-only re-extraction after rebinding. |
| `search_persistence` / `HarvestSession` | Left as is (JSONL stays the harvest record). The ledger adds the queryable, deduplicating layer at the call sites in `missing_clean_doc_search`. |

### 2.2 Considered and not reused

| Table | Why not |
|---|---|
| `evidence_anchors` (0 rows) | Family-level anchor with no file hash, page, offsets, printed text or label source. Adding them would alter a legacy table, which the provenance scheme forbids. |
| `tariff_causal_links` (0 rows) | Version ↔ docket links with `confidence`, no status lifecycle, no clue reference and no review columns. Lineage needs version ↔ version links with stored rule checks. |
| `historical_search_packs`, `missing_doc_remediation_runs` | Per-family packs and per-run summaries, not per-query attempts with outcomes. |
| `tariff_versions.docket_number / order_date / supersedes_label` | **Not written.** The design rules this out; a reviewed back-fill can use the ledger later. |

### 2.3 Deviations from the D1 design

| Design | Implemented | Reason |
|---|---|---|
| `UNIQUE(file_sha256, page, clue_kind, normalized_value)` | `clue_uid` = SHA-256 of (file, page, page-text SHA-256, kind, offsets, value, extractor version) | The design key would collapse distinct printed occurrences. The brief forbids that ("do not collapse evidence solely because normalized values match"). |
| `historical_document_id` / `tariff_version_id` columns on the clue | Separate `clue_associations` table | One page can lie in several bound spans (bundles, duplicate bindings). Per-span columns would duplicate content-addressed clues. |
| `label_source ∈ {source_observed, machine:…}` | Adds `ocr_observed`; separate `normalization` and `verification_status` columns | Text on an OCR page is literally in the artifact, but the artifact is itself a machine reading. `normalized_value` is always a machine interpretation, so its method is recorded on its own. |
| Link status `verified` set by rule | Kept, plus a separate `human_review` column | The brief requires verified links to be distinct from inferred ones. Rule-verified and hand-confirmed are reported separately. |
| Gap statuses `open / resolved_local / resolved_search / searched_not_found` | The brief's lifecycle: `open / candidate_found / verification_required / verified / resolved / searched_not_found` | A local hit never resolves a gap. |
| Candidates stored on the gap row | `gap_candidates` table | A gap can have several candidates, each needing its own review state. |
| Wrapper on `DocumentParamSearcher.search` | Recording at the `missing_clean_doc_search` call sites | GitNexus rates `DocumentParamSearcher.search` **CRITICAL** (8 direct callers, 7 flows, 150 untyped call sites), so it is untouched. `pages_reached` stays NULL for portal searches. `incomplete` is inferred when the result count reaches `max_results`. |
| `lineage typed-gaps-nc` / `resolve-gaps-local` | Under `provenance` | Those commands need explicit `--db` and the production write guard. `lineage` commands use the configured database. |

## 3. Schema changes

Provenance schema **v2 → v3**, additive. Installed only by `provenance migrate`, so **production has no D1 tables**.

| Table | Purpose | Key |
|---|---|---|
| `regulatory_clues` | One printed clue: file SHA-256 (+ `computed` / `stored_unverified`), page, artifact id/version, `text_layer`, page-text SHA-256, offsets, bbox + `bbox_status`, kind, role (`header`/`footer`/`body`/`supersedes`/`index`), `printed_text`, `normalized_value` + `normalized_json`, `normalization`, `label_source`, `verification_status` + reviewer fields, extractor + version | `clue_uid` |
| `clue_scan_pages` | Pages scanned (so "no clue" ≠ "not looked"), with `is_index_page` and scan reason | file, page, text hash, extractor version |
| `clue_associations` | Clue ↔ historical document ↔ version, basis `page_in_bound_span`, document context (status column flagged as unverified evidence, redline fingerprint) | clue, hd, version |
| `lineage_links` | Successor → predecessor, status, `verification_rule`, `rule_checks_json`, `human_review` | `link_uid` |
| `lineage_gaps_typed` | Typed gap, basis, family, utility, anchor / known predecessor / successor, expected revision, docket candidates, window, supporting clue ids, lifecycle status, searched channels, `active` | `gap_uid` |
| `gap_candidates` | Source PDF + SHA-256 + page range, matched clue ids, per-criterion `match_json`, `match_strength`, rationale, review state | `candidate_uid` |
| `search_attempts` | Gap, channel, caller, canonical params + hash, times, result count, pages reached, outcome, error, result refs | id; indexed on `params_hash` |

`drop-schema` removes them (children first). It refuses without `--force` when reviews or search attempts exist, because those cannot be regenerated.

## 4. Clue extraction results [Measured]

**Scope.** Pages inside the 682 bound spans whose PDF has native page artifacts:
- 5,292 distinct pages from 414 PDFs (5,360 page visits; identical files at two paths collapse);
- plus 331 pages scanned for local candidates and 461 for the binding check.

**Not scanned.** 40 PDFs (62 historical documents) have no native page artifacts (web-downloaded leaves never page-mined).

**Rejected artifact rows:** 9,715 with a stale file hash and 7,983 non-native (`docling_page_miner_v1`).

| Kind | Clues |
|---|---:|
| docket | 5,441 |
| utility | 4,794 |
| revision | 4,413 |
| leaf | 4,413 |
| effective_date | 2,414 |
| schedule_code | 2,324 |
| order_date | 2,249 |
| supersedes | 2,059 |
| approval_reference | 1,920 |
| **total** | **30,027** |

| Measure | Value |
|---|---:|
| `source_observed` / `ocr_observed` | 29,544 / 483 |
| normalization: normalized / exact / redline_ambiguous / ocr_repaired / blank / unparsed | 23,351 / 3,571 / 1,616 / 939 / 464 / 86 |
| bbox located / ambiguous (repeated identical text) / no text layer / not attempted (binding-check pages) / not found | 24,486 / 1,249 / 483 / 3,787 / 22 |
| historical documents with clues / with an observed non-utility, non-index clue | 617 / 594 of 682 |
| versions with clues | 475 of 988 |
| versions with a page-anchored docket clue (A4 target ≥ 400) | **450** (393 with the rate-sheet footer form) |
| versions with a readable order date | 226 (M2 baseline stored on versions: 0) |

The 1,616 `redline_ambiguous` clues are the redline text layer printing deleted and inserted values back to back, for example:
- "NC First RevisedOriginal";
- "Sub 12431214";
- "September 30, 2024December 10, 2024".

They keep both candidate readings in `normalized_json` and have **no** `normalized_value`. None feeds a link or a date check.

## 5. Source-grounding validation

- **A3 (automated):** 30,027 / 30,027 clues re-checked. The cited artifact's text hashes to the stored page-text SHA-256, and `text[char_start:char_end] == printed_text`. **0 failures** (`reports/phase2e_d1/source_grounding_all.json`).
- **Page attribution (benchmark):** 199 / 199 matched clues are associated with the right historical document at the gold page.
- **Hand verification** is §11. Gold was read from the page **image**, not the text layer.

## 6. Explicit and verified lineage links [Measured / Rule / Hand-verified]

| | Count |
|---|---:|
| Versions with scanned pages | 448 |
| Version identity from clues: single / none / conflict / redline-only | 308 / 119 / 8 / 13 |
| Versions whose own sheet prints a supersedes reference | 186 (**all** have ≥ 1 link: A5) |
| Versions whose span prints supersedes text that belongs to other sheets only (bundles, joint filings) | 115 (not linked, by rule) |
| Links: `verified` (rule) | **45** (M2 baseline: 5) |
| Links: `candidate` | 5 (several versions carry the predecessor revision) |
| Links: `conflict` | 10 (2 date reversals, 8 sheets printing two supersedes targets: redline old/new) |
| Links: `explicit_unresolved` | 141 (no version carries the printed predecessor, or the printed text is redline glue) |
| **Independently verified links** (hand review of successor + predecessor page images) | **10 confirmed / 0 rejected** of 10 reviewed |

Rule `same_family+printed_revision_match+utility+effective_order+ordinal_order@1`. A link is `verified` only when **all** of these hold:
- exactly one version in the same family carries a source clue for the printed predecessor revision;
- the two sides name the same utility (or neither names one);
- the predecessor's effective date is earlier;
- the successor's own revision is later.

Every check is stored in `rule_checks_json`.

Attribution rules added after measuring bundles:
- A schedule-coded supersedes ("Supersedes Schedule SGS-56") counts only when its base equals the family's schedule code.
- A supersedes naming another leaf counts (as `conflict`) only on a page printing this family's own revision header.
- Pages whose header names the other utility are excluded.

Before these rules, a compliance book bound to one Leaf 571 version produced 103 false "conflicts".

Hand-reviewed links (`bench/clues/sample_50.yaml` → `link_reviews`):

| Links | What was seen |
|---|---|
| leaf-600 ×2, DEC Leaf 99 | Sixth → Fifth Revised Leaf 600 (2025-04-01 > 2025-01-01). Sixty-Second → Sixty-First Revised Leaf 99. |
| leaf-500, leaf-528, leaf-605 | Second → First / First → Original, dates in order |
| R-TOUD-12 → R-TOUD-10B, TSS-48 → TSS-47, CPRE-2 → CPRE-1, Leaf 663 → SRR-7 | Pre-leaf schedule codes. "Supersedes Schedule …" footers match the predecessor sheet titles. |

## 7. Typed gap inventory [Measured]

| Gap type | Basis | Count |
|---|---|---:|
| missing_predecessor_revision | explicit | 111 |
| missing_intermediate_revision | inferred | 121 |
| missing_official_clean_tariff | explicit | 65 |
| missing_successor_document | explicit | 3 |
| missing_regulatory_order | explicit / inferred | 26 / 87 |
| missing_effective_date_evidence | explicit / inferred | 21 / 87 |
| unresolved_version_identity | explicit / inferred | 41 / 119 |
| conflicting_supersession | explicit | 10 |
| **total** | | **691** |

### 7.1 A6 hand review of three reference families

Reviewed against the ledger evidence and the page images viewed in §6, §8.2 and §10.

| Family | Gap | Verdict |
|---|---|---|
| DEP Leaf 600 | predecessor **Fourth Revised** (anchor 6548) | **Correct.** The sheet exists on disk (Dec 1, 2024; hand-verified) but is bound to no version. The window end comes from version 6548's stored `effective_start` 2024-01-24. The Fifth Revised page prints Jan 1, 2025, so that stored date looks wrong (not changed here). |
| | predecessor **Original** (anchor 7265) | Correct (printed "Superseding NC Original Leaf No. 600"); local candidates exist. |
| | clean **First Revised** (anchor 7265, redline) | **Correct given current bindings.** The clean First Revised sheet is hd 7265's PDF pp.96–101, invisible to lineage only because hd 7265 is bound to the index page (§10). |
| | clean **Fifth Revised** (anchor 6548) | **Wrong.** The clean Fifth Revised sheet is inside 6548's own span (f5761884 p.9), alongside its redline. The clean-copy test excludes the anchor version itself. Recorded limitation (§13). |
| | intermediate Second / Third Revised (inferred) | Plausible; inferred only. |
| | identity: hd 7265 / hd 2275 versions | Correct ("every bound page is a tariff index page"). |
| DEP Leaf 602 (JAA) | predecessors JAA-1B, JAA-5, Second Revised Leaf 602 | Correct (printed). Candidates found locally, awaiting review. |
| | conflicting_supersession ×2 (version 6428) | **Correct.** The sheet is the redline (s03): inserted "Superseding NC Rider JAA-6A", struck "Supersedes Schedule JAA-6". |
| | ~20 unresolved identity / missing effective date | **Correct.** Those versions are bound to proposed orders and cover letters, not tariff sheets (benchmark s01, s39, s45, s50). |
| DEC Leaf 99 (`nc-carolinas-rider-SUMMARY`) | predecessors Fourteenth, Fiftieth, Sixtieth | Correct (printed); `searched_not_found` locally. |
| | intermediate 16–49, 52–59 (inferred) | Plausible; the archive holds only 6 Leaf 99 versions. |
| | clean Fifty-First / Sixty-First / Sixty-Second (anchors are redlines) | Correct. Clean Sixty-First and Sixty-Second found on disk and hand-verified (§8.2 #6, #9). |

## 8. Local document discovery results [Measured / Hand-verified]

| | Count |
|---|---:|
| Native pages indexed (newest page-miner version per PDF page) | ≈ 90 k |
| Gaps searched locally | 370 |
| Not locally searchable (identity, effective-date, conflict gaps) / no searchable target | 278 / 43 |
| Gaps with ≥ 1 candidate | **122** |
| Candidates: full / partial | 92 / 103 |
| Candidates dropped for naming the other utility | counted, not stored |
| Gap status after resolution: verification_required / candidate_found / searched_not_found | 55 / 51 / 248 |
| Gaps `verified` (candidate identity hand-confirmed; document located) | **16** |
| **Gaps resolved** | **0** (nothing bound, imported or promoted) |

`missing_regulatory_order` gaps are created only when no local order record exists. Their `searched_not_found` is therefore nearly tautological: the useful search for them is D2's Orders portal.

### 8.1 How candidates are judged

- **full**: the page prints the expected revision, the gap's utility, an effective date in the window, and no redline/proposal marker (glued revision, "(Proposed)", blank order date, or a redline-detector fingerprint).
- **partial**: anything less. A full match moves a gap to `verification_required`, never further.

### 8.2 A7 spot-check

20 candidates, seeded (15 full + 5 partial). Each start page rendered and read (`reports/phase2e_d1/candidate_spotcheck.json`).

| Result | Count |
|---|---:|
| Wrong family | **0** |
| Wrong utility | 0 |
| Correct identity (expected revision, family, utility) | 19 |
| Wrong identity | 1: #19, the "Schedule MROP-16A" match came from **struck** text on the Leaf 661 redline |
| Over-scored | 1: #10 (Original Leaf 660), identity right, but the page is a redline the detector does not flag, so it stays `full` |

Several full candidates are filing exhibits ("EXHIBIT 28/30", "Miller Exhibit 7"). Their identity matches, but whether they are the approved sheet is not established. That is why `verification_required` exists.

## 9. Search ledger results [Measured]

| | Value |
|---|---:|
| Attempts recorded (local resolver, run 1) | 370: 122 `hits`, 248 `no_results` |
| Identical run 2: searches made / avoided | **0 / 370** |
| Distinct parameter hashes | 370 |
| A8: `missing_clean_doc_search` with `search_ledger=` | One row per portal and public-search query, each with a 64-hex `params_hash`. A Cloudflare failure is recorded `blocked` and still propagates. (Stubbed searchers: live NCUC requests are out of scope.) |

Canonical hashing ignores key order, list order, case and whitespace. Local-search parameters include the archive state (native page-artifact count and max id) and the resolver version, so a changed archive is searched again. `blocked` / `error` / `incomplete` never count as already searched. Existing searchers are unchanged; with no ledger passed, `missing_clean_doc_search` behaves exactly as before (its existing tests pass).

## 10. Rebinding rehearsal (`d1_rebind.db`)

Both page ranges were **independently verified** from page images before rebinding:

| | hd 7265 | hd 2275 |
|---|---|---|
| PDF | 7b9171ff (E-2 Sub 1300 Sept-2023) | 1b72e8de (same docket) |
| Bound before | p.1 (Index of Tariffs) | p.1 (Index of Tariffs) |
| Verified run | **pp.96–101** ("Page 1 of 6" … "6 of 6"), p.102 = Leaf 601 | **pp.100–105**, p.106 = Leaf 612 |
| Header | NC First Revised Leaf No. 600 ("OFFICIAL COPY", clean) | NC ~~Original~~ <ins>First Revised</ins> Leaf No. 600: **redline** |
| Effective / docket / order | Oct 1, 2024 / E-2 Sub 1300 / Aug 18, 2023 | "202~~3~~4" / E-2 Sub 1300 / Aug 18, 2023 |

Rebind: `lineage rebind-historical-page-range 7265 --start-page 96 --end-page 101` (and `2275 … 100 105`), unchanged tool. Candidate runs via `provenance extract-candidate` (hybrid). Checker: `bench/clues/rebind_check.py`; output in `reports/phase2e_d1/rebind/`.

| Check | hd 7265 (version 6908) | hd 2275 (version 6056) |
|---|---|---|
| Charges | **125** (= M1.1 fixture) | 136 |
| Evidence pages | all paged rows in 96–101 (9 without a page) | all paged rows in 100–105 (18 without a page) |
| Families | leaf-600 only | leaf-600 only |
| Units | cents rows in `$/kWh`; 6 `$/kW` = JAA demand rates on demand classes | same |
| Duplicate natural keys | 0 | 0 |
| Printed TOTAL reconciliation | **14 / 14** | **3 / 14**: text-layer TOTALs like 6856.76 (struck old total glued to new) |
| Struck-through rows extracted | 0 | **11** "RAL-2 Rider −0.009" rows (one per class), deleted text |
| Run validation | passed | **failed** |
| Promoted | no | no |

**Conclusion.** hd 7265's rebind to 96–101 is correct and its candidate is sound; promotion is a separate approved step. hd 2275 should **not** be re-extracted from its own PDF: it is the redline of the same First Revised sheet. The right production action is to mark it a redline duplicate of hd 7265 (or rebind its version to hd 7265's clean pages), and to fix the redline detector's miss on 1b72e8de.

**Side effect of the existing tool:** `rebind-historical-page-range` **deletes** the version's legacy `tariff_charges` (104 and 136 rows on scratch). The provenance triggers recorded the deletions. Production rebinding must account for this.

**Binding check** (read-only):
- exactly **2** spans are index pages: hd 2275 and hd 7265, with the proposed runs above;
- **18** spans print only other leaf numbers. Examples: hd 7599 bound to leaf-800 spans DEC Service Regulations (benchmark s37); hd 7775 leaf-602 → proposed p.135.

These are review leads (`reports/phase2e_d1/binding_check.json`).

## 11. Benchmark results (`bench/clues/sample_50.yaml`)

**Sample.** 50 documents, seeded stratified sample (`select_sample.py`, seed 20261028):
- DEP / DEC, residential / non-residential / rider / rider summary;
- 5 historical, 20 bundled, 4 OCR, 5 redline-flagged, 8 with no footer clues;
- 12 pages that are not tariff sheets (orders, transcripts, cover letters, exhibits, an index page).

**Gold.** 205 clues, transcribed from rendered page **images**:
- header and footer bands first;
- full page for 5 pages where scoring showed clues outside the bands.

Redline values are transcribed as printed, including struck text (`struck: true`), with redline glue as `ambiguous`.

**Fail-safe.** The scorer exits 1 if any cited PDF is missing, its SHA-256 differs from the gold file, or the page does not exist.

| Kind | Precision | Recall | Initial run (extractor 1.0.0, band-only gold) |
|---|---:|---:|---|
| revision | **1.000** | 0.967 | 1.000 / 0.934 |
| supersedes | **1.000** | 0.964 | 0.962 / 0.926 |
| effective_date | 0.972 | 0.946 | 0.829 / 0.906 |
| docket (footer) | **1.000** | **1.000** | 0.909 / 0.976 |
| order_date (footer) | **1.000** | 0.971 | 1.000 / 0.794 |

| Other measures | Value |
|---|---|
| Page attribution | 199 / 199 |
| Revision / supersedes normalization accuracy (of matched) | 59/59, 27/27 |
| Version identity vs gold ("is this page the family's sheet?") | 48 correct, 1 correctly withheld (redline), **0 wrong** |
| False-positive relationships (hand-reviewed links) | 0 of 10 |
| Incorrect tariff-family associations | 0 among confirmed links and the A7 spot-check. 5 sample tariff sheets are **bound** to the wrong family or utility in the legacy data (s26, s27, s29, s37, s46); the ledger keeps their pages out of the family identity. |

**How the initial run changed.** Two kinds of correction, kept apart:
1. **Gold completeness.** Four pages had clues outside the bands (a struck old footer mid-page on s3; mid-page footers or body effective sentences on s4, s6, s7). These were added after viewing the full page; each is noted in `transcription.yaml`.
2. **Extractor 1.1.0.** Generic fixes for formats the benchmark exposed:
   - "Order dated" with no date, and 2-underscore blanks → `blank`;
   - ", and" in docket lists;
   - "Fifth (Proposed) Revised";
   - "North Carolinas" (sic);
   - "Effective on or after";
   - effective-date `role` by position (body sentences are no longer `footer`).

   Each has a regression test.

**Remaining discrepancies (7), all explained** (`reports/phase2e_d1/clue_benchmark.json`):

| Sample | Discrepancy | Cause |
|---|---|---|
| s37 | revision + supersedes "Leaf A" missed | Letter-numbered leaf on struck pre-2010 DEC Service Regulations. Not supported. |
| s43 | revision (footer), effective, order missed | Hidden OCR layer damage ("UafNo.", "sen-ice", "Older dated June 3.2010") |
| s49 | effective missed | OCR "November I, 2013" |
| s40 | 1 effective FP | Cover-letter heading "Study Effective April 1, 2021" on a non-tariff page |

**Acceptance against the design.**

| Criterion | Result |
|---|---|
| A1 precision ≥ 0.98 per kind | **Met for 4 of 5 kinds.** Effective date 0.972 (1 FP of 36, explained). |
| A2 recall ≥ 0.90 docket / effective, ≥ 0.80 supersedes / revision | **Met** (1.000 / 0.946 / 0.964 / 0.967) |
| A3, A4, A5, A7, A8, A10 | Met (§5, §4, §6, §8, §9, §12) |
| A6 | §7.1 (one gap judged wrong, with the reason) |
| A9 | §10: hd 7265 meets it (125 charges, 14/14 TOTALs). hd 2275 has >0 charges with all evidence in span, but redline contamination. |

**OCR precision.** Only 6 gold clues sit on pages the page miner labels `ocr`. Several scanned DEC sheets carry a hidden OCR text layer labelled `native_text` (s9, s11, s43, s49). OCR quality is therefore not separately measurable from this sample.

## 12. Test results

| Run | Result |
|---|---|
| `tests/test_d1_clue_ledger.py` (new) | **36 passed** |
| `tests/test_missing_clean_doc_search.py`, `test_document_param_search.py`, `test_provenance*.py`, `test_page_integrity.py`, `test_m11_fallback_recovery.py`, `test_docling_page_integrity_extraction.py` | Included in the full suite below |
| Full suite (`reports/phase2e_d1/pytest_full.txt`) | 1,768 passed, **2 failed**, 92 skipped, 4 xfailed (11 min 31 s). Both failures were `test_cli_command_surface.py`: the snapshot and count guard the CLI surface, and D1 adds 5 `provenance` commands. Snapshot updated deliberately (354 → 359), as its docstring prescribes; the file then passes. Net: **1,770 passed, 0 failed** (M1.1: 1,736 passed). |
| 26-document benchmark, 6 configurations vs M1.1 (`bench/phase2d/diff_runs.py --evidence`) | committed cache legacy / docling / native / hybrid: **IDENTICAL**; production artifacts hybrid / docling: **IDENTICAL** |
| M1.1 contamination probe | Not re-run: no extraction code changed, and the benchmark is field- and evidence-identical. |
| Frozen billing baseline | Not run: no billing-relevant code or data changed. Production was untouched; on scratch only `tariff_charges` of the two rebound versions changed, through the existing tool. |
| GitNexus `detect_changes` (scope all) | risk **high**, 3 indexed files, 10 changed symbols: `ensure_provenance_schema`, `drop_provenance_schema` (v3 tables), `search_nc_missing_clean_documents`, `_run_structured_searches`, `_run_keyword_searches` (optional ledger argument), plus line-shift touches (`AUDITED_METADATA_COLUMNS`, `_EVIDENCE`, `_build_structured_query_specs`). Affected flows: 8 `run_nc_missing_doc_workflow_cmd` flows (step 3), 3 `migrate_cmd`, 1 `drop_schema_cmd`. The "high" comes from the missing-doc workflow fan-out; the change there is an optional argument whose default is the old behaviour. **`DocumentParamSearcher.search` (CRITICAL) is not changed.** New modules are additive. |
| Production SHA-256 | before and after `fe563a783cc78d6bb1a561685f1d83c81d7ea4c87729472ede96e192e888cd08`, mtime 2026-10-26 12:36:11, `-wal` 0 bytes: **identical** (`reports/phase2e_d1/production_sha_{before,after}.txt`) |

## 13. Remaining limitations

1. **Redline blindness is inherited.** The fingerprint detector misses some redlines (1b72e8de, the Leaf 660 page). The ledger catches redlines only when their text glues values. Struck text that does not glue still yields clues, so one candidate in 20 matched struck text. D5 (redline change evidence) is the fix.
2. **Clean-copy test excludes the anchor itself.** A span holding both a redline and its clean copy (6548) produces a spurious `missing_official_clean_tariff`.
3. **Version `effective_start` is trusted for windows and date order.** At least one is wrong (6548: 2024-01-24 vs a printed Jan 1, 2025). Links and gaps still record which source they used; no version column is corrected here.
4. **Unsupported formats:** letter leaves ("Leaf A"), OCR-damaged headers ("UafNo."), "November I". The index regex keys on "Index of Tariffs" / "Schedules, Riders, and Other Tariffs" only.
5. **Coverage:** 40 bound PDFs (62 documents) have no native page artifacts and were not scanned. Page-mining them first is cheap.
6. **Portal searches:** `pages_reached` is not recorded, because the CRITICAL-risk searcher was not modified. `incomplete` is inferred from hitting `max_results` only, so silent pagination truncation (M2 §7.2) remains invisible until D2 fixes the searcher.
7. **Gold:** one transcriber, no second pass; band-first transcription missed four pages' mid-page clues (corrected, noted). OCR pages are under-represented.
8. **Human review recorded on scratch only.** The 10 link confirmations and 20 candidate reviews are in `d1.db` and in `sample_50.yaml` / `candidate_spotcheck.json`; production has no ledger.
9. The two rebound documents' **legacy charges were deleted** by the existing rebind tool on scratch (expected behaviour, audited by triggers).

## 14. Recommended D2 scope

1. **Portal reliability** (as planned): the filter-postback wait and result-grid identity assertion in `DocumentParamSearcher.search`, with recorded HTML fixtures. Return pages reached and a stop reason, so the ledger can record real `incomplete` outcomes. This needs its own impact review: the symbol is CRITICAL.
2. **Docket-scoped Orders search** for the 113 `missing_regulatory_order` gaps and the 464 blank order-date clues. Record every attempt in `search_attempts` (channel `portal_orders`).
3. **Network search only for gaps that are `searched_not_found` locally** (248), using the gap's expected revision, docket candidates and window. Skip any `params_hash` already completed.
4. **Reviewed promotion path**:
   - bind the 16 `verified` gaps' documents;
   - correct hd 7265 (rebind) and hd 2275 (mark redline duplicate) in production, with approval;
   - back-fill `tariff_versions.docket_number / order_date / supersedes_label` from observed, non-ambiguous clues as a reviewed step.
5. **Redline detector fix** for 1b72e8de-type bundles, then D5 (inserted/deleted values) using the ledger's `redline_ambiguous` clues as seeds.
6. Page-mine the 40 unscanned PDFs and re-run the ledger. Review the 18 "other-leaf" bindings.
