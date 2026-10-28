# Next Milestone D1: Regulatory Clue Ledger and Local-First Gap Resolution

**Date:** 2026-10-28
**Status:** proposed. Not started.
**Basis:** `docs/reports/2026-10-28_phase2e_m2_discovery_lineage_assessment.md` §2, §3, §8 (items 1–4, 7).
**Scope rule:** additive only. No crawler, no graph database, no LLM step, and no change to acquisition, segmentation or extraction code paths. Live portal work is out of scope (that is D2).

## 1. Problem

The archive already holds most of the evidence needed to link tariff versions, but almost none of it is kept:

| Clue | Printed in the document text | Stored on the version |
|---|---:|---:|
| NCUC docket footer | 448 of 625 documents | 20 |
| Order date | 256 | 0 |
| Supersedes reference | 357 | 33 (5 of 321 resolve to a predecessor) |

Gap reports are not executable. Failed searches leave no trace, so they are repeated. Two bound spans point at a bundle's index page.

## 2. Goal

For every bound tariff span:
1. Record each printed regulatory clue with its exact source location.
2. Turn breaks in the printed supersession chain into **typed gaps**.
3. Try to close each gap from the **local archive** before any network search.
4. Record every search attempt and its outcome.

Inferred and verified relationships stay distinct throughout.

## 3. Design

### 3.1 Clue ledger (one new table; migration on scratch first)

```
regulatory_clues
  id INTEGER PK
  source_pdf TEXT, file_sha256 TEXT, page INTEGER, bbox_json TEXT NULL
  historical_document_id INTEGER NULL, tariff_version_id INTEGER NULL
  clue_kind TEXT   -- docket | order_date | effective_date | revision | supersedes |
                   --   leaf | schedule_code | approval_reference
  printed_text TEXT                 -- exactly as printed (normalised whitespace only)
  normalized_value TEXT             -- e.g. "E-2 Sub 1300", "2023-08-18", "leaf-600:rev-10"
  label_source TEXT                 -- 'source_observed' | 'machine:<extractor@version>'
  extractor TEXT, extractor_version TEXT
  created_at TEXT
  UNIQUE(file_sha256, page, clue_kind, normalized_value)
```

- **Producers:** the existing `heuristics.extract_docket_footer`, `extract_supersedes`, `_extract_summary_effective_date` and the revision-label regex from `importer`, applied **per page** of each bound span over `ncuc_page_artifacts` text. No PDF conversion. bbox comes from pdfplumber words only when the page has a text layer.
- **Rule:** a clue row is `source_observed` only when `printed_text` is a literal substring of that page's text. Anything else (OCR repairs, normalisations that change meaning) is `machine:`.
- **Non-goal:** version columns (`tariff_versions.docket_number`, `order_date`, `supersedes_label`) are **not** written by D1. A later, reviewed step may back-fill them from clues.

### 3.2 Links and typed gaps (derived, rebuildable)

```
lineage_links       -- regenerated from clues; never hand-edited
  from_version_id, to_version_id NULL, link_kind ('supersedes'),
  clue_id, status   -- 'explicit_unresolved' | 'verified' | 'conflict'
  verification_rule TEXT

lineage_gaps_typed
  id, family_key, after_version_id, expected_revision TEXT,
  expected_docket_candidates_json, effective_window_start, effective_window_end,
  evidence_clue_ids_json, status ('open'|'resolved_local'|'resolved_search'|'searched_not_found')
```

A link becomes `verified` only when all three hold:
- the predecessor version exists in the same family;
- its own `revision` clue matches the printed supersedes text;
- its effective date precedes the successor's.

Otherwise it stays `explicit_unresolved`, which produces a typed gap. Gaps also come from date-order discontinuities, but those are marked `inferred`.

### 3.3 Local-first resolution

For each open gap, search the existing page artifacts and fingerprints for:
- the expected revision label;
- leaf / schedule code;
- an effective date inside the window.

Hits are written as candidates:
- `status = resolved_local`;
- a pointer to `(source_pdf, page range)`;
- the matching clue ids.

No binding, no version creation, no extraction promotion. A human or a later reviewed command acts on them.

### 3.4 Search ledger

```
search_attempts
  id, gap_id NULL, channel ('local'|'zoom'|'portal_params'|'portal_orders'|'wayback'),
  params_json, params_hash, started_at, finished_at,
  result_count, pages_reached, outcome ('hits'|'no_results'|'error'|'blocked'),
  error_detail, result_refs_json
```

- Written by the D1 local resolver.
- Also written, through a thin wrapper, by `missing_clean_doc_search` and `DocumentParamSearcher.search` when they run. The wrapper records only; it does not change their behaviour.
- `params_hash` lets gap reports skip searches already tried with the same parameters.

### 3.5 Index-page bindings

A read-only check reports bound spans whose pages are an index ("Index of Tariffs" / "Schedules, Riders, and Other Tariffs") or print a **different** leaf number. For the two confirmed cases (hd 2275, hd 7265), it proposes the corrected page range from the clue ledger (pages whose `leaf` clue matches the family). Applying the rebind uses the existing `lineage rebind-historical-page-range` on a scratch DB, then candidate re-extraction. Production rebinding needs explicit approval.

## 4. Deliverables

1. Migration for `regulatory_clues`, `lineage_links`, `lineage_gaps_typed`, `search_attempts`. It goes into the provenance schema module, so production is not migrated until approved.
2. `provenance build-clue-ledger --db --json` (idempotent; read-only on existing tables).
3. `lineage typed-gaps-nc --db --json` and `lineage resolve-gaps-local --db --json`.
4. Search-ledger wrapper (no behaviour change to searchers).
5. Hand-checked clue sample (`bench/clues/sample_50.yaml`): 50 sheets, every printed footer clue transcribed from the page.
6. Report `docs/reports/<date>_phase2e_d1_clue_ledger.md`.

## 5. Acceptance criteria (all measured on a scratch copy)

| # | Criterion | Target |
|---|---|---|
| A1 | Clue precision on the 50-sheet hand sample (docket, order date, supersedes, revision, effective) | ≥ 0.98 per kind; every miss explained |
| A2 | Clue recall on the same sample | ≥ 0.90 for docket and effective; ≥ 0.80 for supersedes / revision |
| A3 | Every `source_observed` clue's `printed_text` is a substring of its page text | 100% (automated check) |
| A4 | Versions with text that get ≥ 1 page-anchored docket clue | ≥ 400 of the 448 measured |
| A5 | Supersession links: `verified` + `explicit_unresolved` cover all versions that print a supersedes reference | 100%; `verified` > 5 (today's baseline) |
| A6 | Typed gaps for three reference families (DEP leaf-600, DEP leaf-602 JAA, DEC Leaf 99) reviewed by hand | every gap correct, or labelled wrong with the reason |
| A7 | Local resolution: share of typed gaps with a `resolved_local` candidate; spot-check 20 | reported; 0 wrong-family candidates in the spot check |
| A8 | Search ledger: a dry run of `missing_clean_doc_search` writes one `search_attempts` row per query with `params_hash` | 100% of queries |
| A9 | hd 2275 / hd 7265 rebound on scratch; candidate re-extraction | hd 7265 Leaf 600: 125 charges, 11/11 TOTALs (M1.1 fixture values); hd 2275: > 0 charges, all evidence pages in span |
| A10 | No regression | 26-document benchmark field-identical; full test suite; M1.1 contamination probe unchanged; production SHA-256 unchanged |

## 6. Out of scope

- Writing clue values into `tariff_versions` or `historical_documents`.
- Any portal or network request (D2: filter postback fix, stale-grid check, Orders search).
- Segmentation changes (D3/D4).
- Redline change evidence (D5).
- Promotion of any candidate.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Bundle text contains other leaves' footers, so clues get attributed to the wrong span | Clues are per page; attribution to a version requires the page to lie inside the bound span |
| OCR'd footers ("NCUC Dockct No.") | Recorded as `machine:` when normalised; precision measured separately on OCR pages in the sample |
| Revision-label formats differ across eras ("Fifteenth Revised" vs "RR-24") | `normalized_value` keeps both the ordinal and the raw text; unresolved stays `explicit_unresolved`, never guessed |
| Ledger wrapper changes searcher behaviour | Wrapper only records, after the call; existing searcher tests plus one recorded-HTML fixture |
