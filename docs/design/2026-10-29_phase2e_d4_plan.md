# D4: complete NCUC docket inventories

Date: 2026-10-29. Branch: `codex/d4-complete-docket-retrieval`, based on `de376bb`.

## Observed defect and existing infrastructure

The captured E-7 Sub 1026 DocketDetails page contains 100 document rows and
numeric `__doPostBack(grid, 'Page$N')` links for pages 2–6. The session helper
`get_docket_documents` navigates once and parses only that response. D3's
`docket_inventory_outcome` correctly reports `inventory_first_page_only`.
`docket_inventory_search` resolves exactly one docket, invokes the helper,
and returns that first-page outcome. Targeted baseline: 31 D2/D3 discovery
tests passed in 8.80 seconds.

Reuse the existing labelled-cell parser, outcome model, `SearchLedger`,
remote candidates keyed by stable document/target identity, and browser session.
Keep order filtering downstream of complete inventory retrieval. Dates rank
order relevance; they must not filter away inventory records.

## External implementation evaluation

Inspected [WaitingForPower NCUC ingestion](https://github.com/briandgoldberg/WaitingForPower/blob/303970817aa11ca78078221acd6d913635773f5e/src/lib/ingest/ncNcucDockets.ts)
and its [MIT license](https://github.com/briandgoldberg/WaitingForPower/blob/303970817aa11ca78078221acd6d913635773f5e/LICENSE).
Revision: `303970817aa11ca78078221acd6d913635773f5e`. No external code copied.

Relevant techniques:

- Preserve session cookies across all requests. Prefer the existing browser
  context's request client when evaluating HTTP; avoid a second cookie jar.
- Extract all current successful form controls and hidden state from each
  response, including `__VIEWSTATE1` and empty `__VIEWSTATE`. Never guess the
  counter or reuse old state after another request.
- Fresh GET after changing screen URLs. Validate the expected grid and selected
  page independently of the page title; an apparently successful HTTP response
  can contain stale results.
- Two-step filter postback is already addressed by D2. The external Orders
  screen and Show All behavior do not establish DocketDetails paging behavior.
- Its docket-search paging extracts targets from the initial response. That
  alone cannot prove completeness across changing pager windows; discover
  targets from each new response.
- Its textual order disposition rules are outside this milestone and do not
  meet this project's requirements for regulatory authority.

The external header's Cloudflare observations are historical, internally
time-dependent notes. Do not emulate headers to bypass restrictions or retry
challenges. HTTP must use authorized session access, fail closed on challenges,
and retain Playwright as the default/fallback until measured.

## Implementation acceptance criteria

1. Parse the inventory's own pager, current page, numeric targets and counts;
   handle shifting windows without following a backwards ellipsis.
2. Require expected page identity and changed stable row identity before accepting
   a transition. Detect repeated pages, vanished pager/grid, malformed IDs,
   blocked responses, page cap and advertised-count mismatch.
3. Return verified rows on partial failure. Deduplicate by normalized document
   ID. Zero parsed rows without explicit empty evidence is never verified empty.
4. Bound page traversal, postback waits and retries. Do not retry access blocks.
5. Persist enough metadata through the existing search ledger to reconstruct
   docket/method/pages/unique count/completeness/error and document references.
   Reuse candidates rather than add a parallel catalog.
6. Preserve per-page captured evidence and resumable checkpoints where enabled;
   verify the current source before reusing previous pages after interruption.
7. Label derived fixtures as derived. A six-page synthetic sequence is not a
   measured six-page live retrieval. Capture a bounded real regression only
   through existing authorized portal access.
8. Test complete, partial, stale, blocked, retries, cap, deduplication,
   identity/count mismatch, ledger behavior and interruption recovery. Run full
   suite at boundary. Extraction/billing benchmarks become mandatory if those
   paths change; inventory-only work should document their unaffected scope.
9. Record measured before/after completeness and timing, remaining limitations,
   production hash check and GitNexus detected scope before a local commit.

## Current status

Implementation and bounded live validation are recorded in
`docs/reports/2026-10-29_phase2e_d4_complete_docket_retrieval.md`: six pages,
568 entries, 83 public orders, 11 restricted entries preserved separately,
zero PDF downloads. Targeted validation: 180 passed; final full suite and fresh
GitNexus change-scope review are pending. The production-preservation gate failed
during reconnaissance; see the separate incident report. No local commit yet.
