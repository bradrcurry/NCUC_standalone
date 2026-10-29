# D4 — complete docket inventory retrieval

Date: 2026-10-29. Branch: `codex/d4-complete-docket-retrieval`, based on `de376bb`.

**Status:** implementation, bounded live validation, final full suite and reviewed
change scope passed. Changes are delivered locally on the branch above. The production-preservation
gate **failed during reconnaissance**; see the safety incident below. This report
does not claim the entire milestone or long-running goal is complete.

## Measured retrieval results

One bounded browser traversal of E-7 Sub 1026 verified all six inventory pages.
It took **18.73 seconds from an already loaded first page**, including parsing
and sanitized checkpoint writes. Login, docket resolution and the initial page
navigation are excluded from that timing. No PDF was downloaded.

| Inventory page | Entries | Typed Order | Restricted entries |
| --- | ---: | ---: | ---: |
| 1 | 100 | 33 | 0 |
| 2 | 100 | 21 | 11 |
| 3 | 100 | 8 | 0 |
| 4 | 100 | 10 | 0 |
| 5 | 100 | 8 | 0 |
| 6 | 68 | 5 | 0 |
| Total | **568** | **85** | **11** |

There are 557 public document GUIDs and 11 separately labeled restricted-entry
identities derived from visible attachment references. Two of the 85 orders are
restricted; 83 are eligible for public candidate discovery. A listing identity
derived from attachment references is **not** presented as a recovered portal
DocumentId.

The previous reader exposed 100/568 visible entries and 33/83 public orders.
The corrected path exposes all 568 entries and 83 public order candidates:
**468 additional entries and 50 additional public orders**, without downloading
or promoting any document. The portal prints no independent item total here;
completion is based on verified sequential pages, the advertised pager, changed
row signatures, and the terminal page. This is not an atomic server snapshot.

Source-observed listings beyond page 1 include:

- Page 2, 10/25/2013: “Order Approving Rate Schedules”,
  `dfcd0894-add8-4504-9d42-b8e9d0cb26ee`.
- Page 3, 9/24/2013: “Order Granting General Rate Increase”,
  `413b681f-f9ba-4bfe-9b73-a410602aaffb`.

Those statements describe the inventory's titles/dates/IDs. The PDFs were not
read, their substantive relevance was not independently verified, and no
regulatory approval or tariff authority was inferred.

Evidence: `reports/phase2e_d4/browser_validation/`, and the six committed,
sanitized fixtures under `tests/fixtures/d4_portal/`.

## Root causes and fixes

1. `session.get_docket_documents` navigated once and returned a list from page 1.
   D3 correctly labeled its inventory outcome partial, but never traversed the
   remaining pages. The session reader also had a generic table/cell parser
   that lost descriptions. The shared reader now delegates to the existing D3
   labeled-cell parser and the same verified walker used by discovery.
2. A postback can be navigating after a click returns. Playwright can reject
   `page.content()` during that transition; waiting for network idle alone also
   produced an unchanged page during reconnaissance. The walker polls boundedly
   for both the expected selected page and new stable row identities, tolerating
   transient navigation-read failures.
3. Page 2 contains 11 entries explicitly marked confidential. Their disabled
   detail anchors have no `href`; the old parser raised `KeyError`. The parser
   now preserves their visible restriction evidence. Stable, namespaced entry
   keys use the visible file references while leaving `document_id` empty.
   Restricted rows never become public download candidates, and the shared
   compatibility list withholds their attachment URLs.
4. A successful partial outcome's error detail was dropped by the ledger. It is
   now retained in the existing `search_attempts.error_detail` column.
5. Canonical CLI search/fetch and smoke paths used the shared list reader too.
   Fixing only D3's network-discovery wrapper would leave them incomplete. The
   shared list API now returns a complete `list` subclass with its outcome, or
   raises `NcucSessionError` carrying a verified partial outcome. An explicitly
   named first-page loader lets the bounded discovery adapter own its cap and
   per-page capture directory without traversing twice.

## Changed modules and contracts

| Module | Change |
| --- | --- |
| `historical/ncuc/docket_inventory.py` | Traversal adapter, pager/window parsing, docket identity checks, stable signatures, deduplication, caps, bounded retries, blocked-response stop, sanitized atomic checkpoints, legacy list adapter. |
| `historical/ncuc/remote_discovery.py` | Version 1.2.0; existing search delegates to verified traversal; handles complete/partial shared providers; preserves restriction metadata; excludes restricted entries from public candidates. |
| `historical/ncuc/session.py` | Existing shared reader uses the same parser/walker; complete list contract and explicit partial exception; explicit first-page loader for bounded adapters. No authentication or download routine changed. |
| `historical/ncuc/search_ledger.py` | Existing columns retain error details and namespaced restricted-entry references. No migration or new catalog. |
| `cli_commands/provenance.py` | Existing `--capture-dir` enables per-query inventory checkpoints; bounded executor uses the explicit first-page loader. |
| `document_intelligence/acquisition.py` | Fetch inventory uses `mode=ro` and `query_only`, preventing the reconnaissance preflight defect from recurring in that helper. |
| `bench/ncuc_inventory_replay.py` | Offline completeness/timing and partial-to-complete ledger/candidate recovery benchmark using committed real captures and a new temporary database. |

The legacy list contains the same five document fields. It remains JSON/list
compatible. `inventory_outcome` is attached as metadata. Partial inventories now
raise instead of being silently represented as complete lists; advanced callers
can inspect the exception's `.outcome`. Unknown grid/pager syntax, missing stable
identity, wrong docket, vanished pager, stale rows, and unmatched counts remain
errors or partial results, never verified negative evidence.

The wrapper's `max_pages` bounds its own traversal. A custom provider that
already returns a complete inventory owns its own request limits; the canonical
CLI uses the explicit first-page provider so its traversal stays within the
adapter's bound.

## Persistence, interruption and recovery

No new document catalog or database migration was introduced. Existing query
parameters preserve docket and method; search attempts preserve page count,
verified sequential page coverage, unique entry count, stop reason, errors and
entry references. Existing remote candidates retain their per-target identity
and review state.

With `--capture-dir`, each query has sanitized page HTML and an atomic
`inventory.json` containing retrieval version, docket, method, advertised pages,
verified pages, unique and restricted counts, completion status and errors.
Unverified transition evidence is kept in a separate directory. Hidden form
state and executable script bodies are blanked; cookies/credentials are absent.

Checkpoints survive interruption. Recovery currently **replays and revalidates
every current page** and upserts candidates; it does not skip earlier portal
requests using cached HTML. This deliberate limitation avoids treating an old
portal state as current. Completed identical searches are skipped by the
existing ledger. Restricted IDs remain separate from public DocumentIds.

The offline benchmark executes a first-page partial attempt, a complete replay,
then an identical completed query. It produces 83 candidate rows total, two
attempts (`incomplete`, `hits`), and avoids the third query. No candidate was
promoted, bound, human-reviewed, or given regulatory authority.

## External techniques and HTTP evaluation

Reviewed [WaitingForPower's NCUC source](https://github.com/briandgoldberg/WaitingForPower/blob/303970817aa11ca78078221acd6d913635773f5e/src/lib/ingest/ncNcucDockets.ts)
and [MIT license](https://github.com/briandgoldberg/WaitingForPower/blob/303970817aa11ca78078221acd6d913635773f5e/LICENSE).
No external code was copied. Relevant techniques: preserve session cookies,
refresh custom `__VIEWSTATE1` from every response, make a fresh GET when changing
screens, and verify the result grid independently of the title. Its Orders
screen/Show All behavior is not evidence of DocketDetails paging semantics.

The existing browser continues submitting current ASP.NET form state. A bounded
HTTP experiment used that browser context's authenticated request client. Its
first GET returned **403 in 0.129 seconds**. There was no HTTP postback, retry,
header-spoofing escalation, challenge solving, or fallback attempt against the
denial. HTTP is therefore **not adopted**. The unsuccessful GET timing is not a
usable-throughput benchmark; browser retrieval remains the supported path.

Reconnaissance and debugging attempts are retained separately as partial/error
evidence. Earlier failures are not rewritten as successful searches. No broad
crawl was run, and no PDFs were downloaded in any D4 probe.

## Validation

- Pre-change discovery baseline: 31 passed in 8.80 s.
- Pre-change full suite: 1,857 passed, 92 skipped, 4 xfailed in 767.69 s.
- An intermediate full run passed 1,884 tests before the final pager review and
  shared-session correction. It is not the final implementation's suite result.
- Final targeted suite: **180 passed**, 44 warnings, 30.61 s.
- Final full suite: **1,894 passed, 92 skipped, 4 xfailed**, 135 warnings,
  **670.54 s**. Existing skip markers remained active; no live-DB snapshot was
  configured. Ruff passed for the new traversal, benchmark and D4 tests. Git's
  staged whitespace check passed after formatting-only cleanup of the captures.
- Offline benchmark: 568 entries, 83 public order candidates; no duplicate
  candidate rows after partial-to-complete recovery; one repeated query avoided.
  Timings are saved in `reports/phase2e_d4/inventory_benchmark.json` and must be
  interpreted as local parsing/replay, not network throughput.
  Five-run median: **0.0624 s** for the old one-page reader (100 entries, partial)
  and **0.9212 s** for the verified six-page replay (568 entries, complete).
  These perform different amounts of work; no speedup is claimed.
- Regression cases include all six real pages, synthetic changing pager windows,
  unsupported postback syntax, both quote styles, stale/repeated pages, caps,
  blocked pages without retry, count changes, missing identity, wrong docket,
  transient/permanent navigation errors, interruption and revalidation,
  restricted-entry identity/download exclusion, ledger errors, and shared-reader
  complete/partial contracts.
- Extraction and billing code/data are unchanged. No extraction engine, OCR,
  Docling dependency/model, charge writer or tariff-selection change was made.
  Extraction/billing benchmarks were not rerun for this inventory-only change.

GitNexus refresh needed an existing compatible Windows runtime, Git's DLL search
path, one worker and a 1 GiB database buffer pool. A full rebuild succeeded.
Impact checks rated the initial discovery/parser/ledger edits LOW. The shared
session reader was **HIGH**, with four direct canonical/acquisition callers;
this was reported before editing, and list/exception contracts were tested.
Some dynamic CLI callbacks are UNKNOWN in the graph and were inspected directly.
The incremental refresh dropped four symbols and misattributed unrelated flows
to a new variable. A full rebuild restored the symbols. Final `detect_changes`
returned no error/partial flag: **16 expected execution flows**, with CRITICAL
aggregate risk across shared session/ledger changes and added symbols. The
application symbols match the independent AST diff; `check_identity` is
unchanged. The actual affected flows are docket fetching, bounded network
searching, local-resolution ledger recording, and read-only fetch inventory.
Evidence: `gitnexus_detect_final_full.json`, `session_final_full_context.json`,
`ast_scope_review.json`, and `gitnexus_final_full_analyze.txt` under the D4
report directory. No low-risk verdict is claimed for the aggregate change.

## Production safety incident and remaining limits

The documented zero-cycle “dry run” preflight opened production through the
migration-capable connection helper during reconnaissance. Its file hash changed
from `fe563a783cc78d6bb1a561685f1d83c81d7ea4c87729472ede96e192e888cd08`
to `0f6f8f3551f21908533019c5f568d49e254fdd0149fa65ceedef9aa37031b6ca`.
This violated the user's boundary and was disclosed promptly. No restoration or
subsequent production write was attempted.

All 79 shared application tables match the preserved D1 research copy row for
row; only the shared internal sequence table differs. That historical reference
is not an exact pre-command snapshot. A later implementation hash still matches
the post-incident hash. Do not claim byte-identical production preservation for
this milestone. Full evidence and containment are in
`docs/reports/2026-10-29_d4_preflight_safety_incident.md`.

Other limits: recovery replays earlier pages; portal inventories have no atomic
snapshot guarantee; unsupported layouts remain partial; restricted entries
without stable visible identity cannot be guessed; HTTP is unavailable under
the tested access; discovered order contents still require review. No human
verification, historical correction, production promotion, push or merge took
place. D5 is the next implementation milestone; the failed preservation gate
remains explicitly recorded independently of the validated source delivery.
